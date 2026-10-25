import hashlib
import hmac
import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from config import ALLOWED_USER_IDS, LOG_SALT

logger = logging.getLogger(__name__)

# Шаблоны ключей: всё, что похоже на секрет, в логах заменяется на ***
SECRET_PATTERNS = [
    re.compile(r"\d+:[A-Za-z0-9_-]{30,}"),    # Telegram bot token
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),     # Google API key
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),      # Groq key
    re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"),  # OpenRouter key
]


class SafeFormatter(logging.Formatter):
    """Маскирует ключи и соль во всей строке лога, включая traceback."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        text = text.replace(LOG_SALT, "***")
        for pattern in SECRET_PATTERNS:
            text = pattern.sub("***", text)
        return text


def anon_id(user_id: int) -> str:
    """HMAC с секретной солью: по логу нельзя восстановить user_id, но один пользователь узнаётся по одному ID."""
    return hmac.new(LOG_SALT.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:12]


_ALLOWED_CONTROL = {"\n", "\t"}


def sanitize_user_text(text: str) -> str:
    """Нормализует Unicode и удаляет управляющие символы (в том числе \\x00, который ломает SQLite и логи)."""
    normalized = unicodedata.normalize("NFC", text)
    cleaned = "".join(
        ch for ch in normalized
        if ch in _ALLOWED_CONTROL or unicodedata.category(ch) != "Cc"
    )
    return cleaned.strip()


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из ALLOWED_USER_IDS. Пустой список = доступ открыт."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if ALLOWED_USER_IDS:
            user = data.get("event_from_user")
            if user is None or user.id not in ALLOWED_USER_IDS:
                logger.warning("Отказ в доступе, uid=%s", anon_id(user.id) if user else "unknown")
                return None
        return await handler(event, data)


class RateLimiter:
    """Скользящее окно: не больше max_calls вызовов от одного пользователя за window секунд."""

    def __init__(self, max_calls: int, window: float) -> None:
        self.max_calls = max_calls
        self.window = window
        self._calls: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        calls = self._calls[user_id]
        while calls and now - calls[0] > self.window:
            calls.popleft()
        if len(calls) >= self.max_calls:
            return False
        calls.append(now)
        return True

    def prune(self) -> None:
        """Удаляет пользователей без записей в окне, иначе словарь растет бесконечно."""
        now = time.monotonic()
        for user_id in list(self._calls):
            calls = self._calls[user_id]
            while calls and now - calls[0] > self.window:
                calls.popleft()
            if not calls:
                del self._calls[user_id]


def check_length(text: str, limit: int) -> str | None:
        """Возвращает текст ошибки, если сообщение длиннее лимита, иначе None."""
        if len(text) <= limit:
            return None
        return (
            f"Сообщение слишком длинное: {len(text)} из {limit} символов. "
            "Сократи его или разбей на несколько сообщений."
        )