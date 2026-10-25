import asyncio
import logging
import random
import sys
import time
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import ErrorEvent, Message
from aiogram.utils.chat_action import ChatActionSender

from config import (
    HISTORY_LIMIT, MAX_INPUT_CHARS, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW,
    RETENTION_DAYS, STREAM_EDIT_INTERVAL, TELEGRAM_TOKEN, TG_MAX_LEN,
)
from database import (
    add_user, clear_user_history, delete_user_data, get_user_history,
    init_db, purge_old_messages, save_message,
)
from prompts import BUSY_MESSAGES, FALLBACK_MESSAGES, RATE_LIMIT_MESSAGES
from providers import check_providers, generate_reply
from security import AccessMiddleware, RateLimiter, SafeFormatter, sanitize_user_text
from security import AccessMiddleware, RateLimiter, SafeFormatter, anon_id, sanitize_user_text
from security import AccessMiddleware, RateLimiter, SafeFormatter, anon_id, check_length, sanitize_user_text

logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.message.outer_middleware(AccessMiddleware())

rate_limiter = RateLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)

# Замки на пользователя. Их не удаляем: удаление замка, на который уже есть ссылка,
# позволяет двум запросам одного пользователя выполняться параллельно.
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Пользователи, уже записанные в БД. Чтобы не делать add_user на каждое сообщение.
known_users: set[int] = set()


async def ensure_user(user_id: int) -> None:
    if user_id not in known_users:
        await add_user(user_id)
        known_users.add(user_id)


async def show_stream_text(placeholder, state: dict, text: str, force: bool = False) -> None:
    """Обновляет сообщение-заглушку, не чаще STREAM_EDIT_INTERVAL секунд (кроме force)."""
    now = time.monotonic()
    if not force and now - state["last_edit"] < STREAM_EDIT_INTERVAL:
        return
    shown = text[:TG_MAX_LEN]
    if not shown.strip() or shown == state["last_text"]:
        return
    try:
        await placeholder.edit_text(shown, parse_mode=None)
        state["last_text"] = shown
        state["last_edit"] = now
    except TelegramAPIError as err:
        logger.debug("Не удалось обновить сообщение: %s", type(err).__name__)

async def answer_with_stream(message: Message, user_id: int, user_text: str) -> None:
    placeholder = await message.answer("…", parse_mode=None)
    state = {"last_edit": 0.0, "last_text": ""}

    async def on_text(text: str) -> None:
        await show_stream_text(placeholder, state, text)

    try:
        history = await get_user_history(user_id, limit=HISTORY_LIMIT)
        reply = await generate_reply(
            history + [{"role": "user", "content": user_text}],
            on_text=on_text,
        )
    except Exception:
        logger.exception("Все провайдеры упали, uid=%s", anon_id(user_id))
        await show_stream_text(placeholder, state, random.choice(FALLBACK_MESSAGES), force=True)
        return  # в БД ничего не пишем: диалога не было

    await show_stream_text(placeholder, state, reply, force=True)
    for start in range(TG_MAX_LEN, len(reply), TG_MAX_LEN):
        await message.answer(reply[start:start + TG_MAX_LEN], parse_mode=None)

    try:
        await save_message(user_id, "user", user_text)
        await save_message(user_id, "model", reply)
    except Exception:
        logger.exception("Не удалось сохранить диалог, uid=%s", anon_id(user_id))


@dp.message(CommandStart())
async def command_start(message: Message) -> None:
    await ensure_user(message.from_user.id)
    welcome_text = (
        f"Привет, {html.bold(html.quote(message.from_user.first_name))}! 💙\n\n"
        "Я здесь, чтобы выслушать и поддержать тебя в это непростое время. "
        "Расставание — это больно, но мы пройдем через это вместе.\n\n"
        f"Важно: я храню нашу переписку, чтобы помнить контекст, и удаляю её через {RETENTION_DAYS} дней. "
        "Команда /reset очищает историю, /delete_me удаляет все данные полностью.\n\n"
        "Расскажи, что у тебя на душе? Что сейчас происходит?"
    )
    await message.answer(welcome_text)


@dp.message(Command("reset"))
async def command_reset(message: Message) -> None:
    await clear_user_history(message.from_user.id)
    await message.answer("Мы начали наш диалог с чистого листа. Расскажи, о чем думаешь сейчас?")


@dp.message(Command("delete_me"))
async def command_delete_me(message: Message) -> None:
    user_id = message.from_user.id
    await delete_user_data(user_id)
    known_users.discard(user_id)  # иначе следующий запрос пойдет без записи пользователя в БД
    await message.answer("Все твои данные удалены. Если захочешь вернуться, просто напиши мне снова.")


@dp.message(F.text)
async def handle_chat_message(message: Message) -> None:
    user_id = message.from_user.id
    user_text = sanitize_user_text(message.text)

    if not user_text:
        return

    length_error = check_length(user_text, MAX_INPUT_CHARS)
    if length_error:
        await message.answer(length_error, parse_mode=None)
        return

    # Сначала проверяем занятость: отклоненное сообщение не должно тратить лимит
    lock = user_locks[user_id]
    if lock.locked():
        await message.answer(random.choice(BUSY_MESSAGES), parse_mode=None)
        return

    if not rate_limiter.allow(user_id):
        await message.answer(random.choice(RATE_LIMIT_MESSAGES), parse_mode=None)
        return

    # Между проверкой locked() и захватом замка нет await, поэтому гонки не будет
    async with lock, ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        await ensure_user(user_id)
        await answer_with_stream(message, user_id, user_text)


@dp.error()
async def on_unhandled_error(event: ErrorEvent) -> bool:
    """Последний рубеж: бот не падает, в лог пишется только тип ошибки."""
    logger.error(
        "Необработанное исключение: %s",
        type(event.exception).__name__,
        exc_info=event.exception,
    )
    message = event.update.message
    if message is not None:
        try:
            await message.answer(random.choice(FALLBACK_MESSAGES), parse_mode=None)
        except Exception:
            logger.warning("Не удалось отправить запасной ответ")
    return True


async def housekeeping() -> None:
    """Раз в час удаляет старые сообщения и обрезает словарь лимитов."""
    while True:
        try:
            removed = await purge_old_messages(RETENTION_DAYS)
            # Очистка могла удалить запись пользователя, поэтому кэш сбрасываем
            known_users.clear()
            rate_limiter.prune()
            logger.info("Очистка выполнена, удалено сообщений: %d", removed)
        except Exception:
            logger.exception("Ошибка фоновой очистки")
        await asyncio.sleep(3600)


async def main() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    # Эти библиотеки пишут URL с токеном бота и тексты апдейтов на уровне INFO
    for noisy in ("httpx", "httpcore", "aiogram.event", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    await init_db()
    await check_providers()
    cleaner = asyncio.create_task(housekeeping())
    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        cleaner.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())