import os
import re

from dotenv import load_dotenv

load_dotenv()

_PLACEHOLDER_PREFIXES = ("your_", "your-", "changeme", "xxx")


def _read(name: str, required: bool = False, default: str = "") -> str:
    value = os.getenv(name, "").strip() or default
    if not value:
        if required:
            raise RuntimeError(f"[config] Не задана переменная {name}. Добавьте её в .env")
        return ""
    if value.lower().startswith(_PLACEHOLDER_PREFIXES):
        raise RuntimeError(f"[config] {name} содержит заглушку из шаблона. Укажите реальное значение")
    return value


TELEGRAM_TOKEN = _read("TELEGRAM_TOKEN", required=True)
if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", TELEGRAM_TOKEN):
    raise RuntimeError("[config] TELEGRAM_TOKEN имеет неверный формат (ожидается 123456:ABC...)")

GEMINI_API_KEY = _read("GEMINI_API_KEY", required=True)
GROQ_API_KEY = _read("GROQ_API_KEY")
OPENROUTER_API_KEY = _read("OPENROUTER_API_KEY")

# Имена моделей задаются в .env: смена модели не требует правки кода.
# Значения по умолчанию проверьте по актуальной документации провайдеров.
GEMINI_MODEL = _read("GEMINI_MODEL", default="gemini-2.5-flash")
GROQ_MODEL = _read("GROQ_MODEL", default="openai/gpt-oss-120b")
OPENROUTER_MODEL = _read("OPENROUTER_MODEL", default="meta-llama/llama-3.3-70b-instruct:free")

# Соль для обезличивания user_id в логах. Сгенерировать: python3 -c "import secrets; print(secrets.token_hex(32))"
LOG_SALT = _read("LOG_SALT", required=True)
if len(LOG_SALT) < 32:
    raise RuntimeError("[config] LOG_SALT слишком короткий: нужно не меньше 32 символов")

try:
    ALLOWED_USER_IDS = frozenset(
        int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
    )
except ValueError:
    raise RuntimeError("[config] ALLOWED_USER_IDS должен содержать только числа через запятую")

# --- Лимиты и настройки ---
MAX_INPUT_CHARS = int(_read("MAX_INPUT_CHARS", default="500")) # максимум символов в одном сообщении пользователя
MAX_CONCURRENT_LLM = 5          # одновременных запросов к провайдерам на весь бот
RETENTION_DAYS = 30             # срок хранения переписки
HISTORY_LIMIT = 10              # сколько последних сообщений передаем модели
TG_MAX_LEN = 4096               # лимит одного сообщения Telegram
STREAM_EDIT_INTERVAL = 1.0      # минимальная пауза между правками сообщения, сек
RATE_LIMIT_MAX = 5              # сообщений от одного пользователя...
RATE_LIMIT_WINDOW = 10.0        # ...за столько секунд