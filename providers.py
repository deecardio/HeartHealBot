import asyncio
import logging
import time

import openai
from google import genai
from google.genai import types as gemini_types
from openai import AsyncOpenAI

from config import (
    GEMINI_API_KEY, GEMINI_MODEL, GROQ_API_KEY, GROQ_MODEL,
    MAX_CONCURRENT_LLM, OPENROUTER_API_KEY, OPENROUTER_MODEL,
)
from prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

MODELS = {
    "gemini": GEMINI_MODEL,
    "groq": GROQ_MODEL,
    "openrouter": OPENROUTER_MODEL,
}
PROVIDER_ORDER = ["gemini", "groq", "openrouter"]

MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 2.0       # 2 -> 4 -> 8 секунд
RETRYABLE_CODES = {429, 500, 502, 503, 504}
PERMANENT_CODES = {401, 403, 404}  # неверный ключ или модель: повтор не поможет
REQUEST_TIMEOUT = 60.0          # сек на один вызов провайдера, включая стриминг
BREAKER_COOLDOWN = 300.0        # сек паузы после временного сбоя

llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)


class EmptyReplyError(Exception):
    """Модель вернула пустой ответ (например, сработал фильтр безопасности)."""


class ProviderConfigError(Exception):
    """Ошибка конфигурации провайдера: ключ или модель. Отключаем до перезапуска."""


gemini_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=gemini_types.HttpOptions(timeout=int(REQUEST_TIMEOUT * 1000)),  # в миллисекундах
)
groq_client = (
    AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=REQUEST_TIMEOUT)
    if GROQ_API_KEY else None
)
openrouter_client = (
    AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1", timeout=REQUEST_TIMEOUT)
    if OPENROUTER_API_KEY else None
)

GEMINI_CONFIG = gemini_types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    temperature=0.7,
    max_output_tokens=800,
)


def status_code(err: BaseException) -> int | None:
    code = getattr(err, "status_code", None) or getattr(err, "code", None)
    return code if isinstance(code, int) else None


class CircuitBreaker:
    """Временная пауза после сбоя и постоянное отключение при ошибке конфигурации."""

    def __init__(self, cooldown: float) -> None:
        self.cooldown = cooldown
        self._open_until: dict[str, float] = {}
        self._disabled: dict[str, str] = {}

    def is_open(self, name: str) -> bool:
        if name in self._disabled:
            return True
        until = self._open_until.get(name)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._open_until[name]
            return False
        return True

    def trip(self, name: str) -> None:
        self._open_until[name] = time.monotonic() + self.cooldown
        logger.warning("Провайдер %s на паузе %.0f сек", name, self.cooldown)

    def disable(self, name: str, reason: str) -> None:
        self._disabled[name] = reason
        logger.error("Провайдер %s отключен до перезапуска: %s", name, reason)


breaker = CircuitBreaker(BREAKER_COOLDOWN)


async def stream_gemini(contents: list, on_text) -> str:
    parts: list[str] = []
    stream = await gemini_client.aio.models.generate_content_stream(
        model=MODELS["gemini"],
        contents=contents,
        config=GEMINI_CONFIG,
    )
    async for chunk in stream:
        if chunk.text:
            parts.append(chunk.text)
            await on_text("".join(parts))
    return "".join(parts)


async def stream_openai(client: AsyncOpenAI, model: str, messages: list[dict], on_text) -> str:
    parts: list[str] = []
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.7,
        max_tokens=800,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            parts.append(delta)
            await on_text("".join(parts))
    return "".join(parts)


async def call_provider(name: str, history_records: list[dict], on_text) -> str:
    if name == "gemini":
        contents = [
            gemini_types.Content(
                role="user" if h["role"] == "user" else "model",
                parts=[gemini_types.Part.from_text(text=h["content"])],
            )
            for h in history_records
        ]
        return await stream_gemini(contents, on_text)

    client = groq_client if name == "groq" else openrouter_client
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + [
        {"role": "user" if h["role"] == "user" else "assistant", "content": h["content"]}
        for h in history_records
    ]
    return await stream_openai(client, MODELS[name], messages, on_text)


def is_enabled(name: str) -> bool:
    if name == "groq":
        return groq_client is not None
    if name == "openrouter":
        return openrouter_client is not None
    return True


def is_retryable(err: Exception) -> bool:
    if isinstance(err, (EmptyReplyError, ProviderConfigError)):
        return False
    if isinstance(err, (asyncio.TimeoutError, TimeoutError, ConnectionError, openai.APIConnectionError)):
        return True
    return status_code(err) in RETRYABLE_CODES


async def with_retries(name: str, history_records: list[dict], on_text) -> str:
    delay = INITIAL_RETRY_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            text = await asyncio.wait_for(
                call_provider(name, history_records, on_text),
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as err:
            code = status_code(err)
            if code in PERMANENT_CODES:
                raise ProviderConfigError(f"{name}: код {code}") from err
            logger.warning(
                "Ошибка у %s (попытка %d/%d): %s, код %s",
                name, attempt, MAX_RETRIES, type(err).__name__, code,
            )
            if not is_retryable(err) or attempt == MAX_RETRIES:
                raise
            await asyncio.sleep(delay)
            delay *= 2
            continue

        if not text:
            raise EmptyReplyError(f"Пустой ответ от {name}")
        return text

    raise RuntimeError("Недостижимо: цикл повторов завершился без результата")


async def check_providers() -> None:
    """Проверяет при старте, что модели существуют. Не тратит квоту на генерацию."""
    try:
        await gemini_client.aio.models.get(model=MODELS["gemini"])
    except Exception as err:
        code = status_code(err)
        if code in PERMANENT_CODES:
            breaker.disable("gemini", f"модель {MODELS['gemini']} недоступна, код {code}")
        else:
            logger.warning("Не удалось проверить Gemini: %s", type(err).__name__)

    for name, client in (("groq", groq_client), ("openrouter", openrouter_client)):
        if client is None:
            continue
        try:
            listing = await client.models.list()
        except Exception as err:
            logger.warning("Не удалось проверить %s: %s", name, type(err).__name__)
            continue
        available = {m.id for m in listing.data}
        if MODELS[name] not in available:
            breaker.disable(name, f"модель {MODELS[name]} не найдена в списке провайдера")


async def generate_reply(history_records: list[dict], on_text) -> str:
    for name in PROVIDER_ORDER:
        if not is_enabled(name):
            continue
        if breaker.is_open(name):
            logger.info("Провайдер %s пропущен", name)
            continue
        try:
            async with llm_semaphore:
                return await with_retries(name, history_records, on_text)
        except EmptyReplyError:
            # Фильтр безопасности, а не сбой провайдера: не отключаем его
            logger.warning("Пустой ответ от %s, пробуем следующего", name)
        except ProviderConfigError as err:
            breaker.disable(name, str(err))
        except Exception as err:
            logger.error("Провайдер %s недоступен: %s", name, type(err).__name__)
            breaker.trip(name)

    raise RuntimeError("Все доступные нейросети упали или недоступны.")