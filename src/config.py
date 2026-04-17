"""
Конфигурация бота (модуль для импорта из src/).

Загрузка переменных из .env, парсинг JSON (USERS, маппинги комнат),
валидация USERS, справочники статусов/приоритетов Redmine.

load_dotenv() вызывается здесь — это единственный источник загрузки .env.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from utils import set_timezone

load_dotenv()

logger = logging.getLogger("redmine_bot")

# ═══════════════════════════════════════════════════════════════
# ПУТИ
# ═══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent.parent  # Корень проекта
DATA_DIR = BASE_DIR / "data"  # State-файлы JSON


def want_log_file() -> bool:
    """Писать ли лог в файл (stdout всегда). Отключение: LOG_TO_FILE=0|false|no|off."""
    v = os.getenv("LOG_TO_FILE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def resolved_log_file() -> Path:
    """
    Путь к файлу лога. LOG_PATH — абсолютный или относительно корня проекта (BASE_DIR).
    По умолчанию: data/bot.log.
    """
    raw = os.getenv("LOG_PATH", "").strip()
    if raw:
        p = Path(os.path.expanduser(raw))
        return p if p.is_absolute() else BASE_DIR / p
    return DATA_DIR / "bot.log"


LOG_FILE = resolved_log_file()


def log_file_max_bytes() -> int:
    """Максимальный размер `bot.log` до ротации (`RotatingFileHandler`). По умолчанию 5 МБ."""
    raw = os.getenv("LOG_MAX_BYTES", "").strip()
    if not raw:
        return 5 * 1024 * 1024
    try:
        return max(1024, int(raw))
    except ValueError:
        return 5 * 1024 * 1024


def log_file_backup_count() -> int:
    """Число архивных файлов `bot.log.1` … (минимум 1, иначе ротация бессмысленна). По умолчанию 5."""
    raw = os.getenv("LOG_BACKUP_COUNT", "").strip()
    if not raw:
        return 5
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


# ═══════════════════════════════════════════════════════════════
# MATRIX
# ═══════════════════════════════════════════════════════════════

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER")
MATRIX_ACCESS_TOKEN = os.getenv("MATRIX_ACCESS_TOKEN")
MATRIX_USER_ID = os.getenv("MATRIX_USER_ID")
MATRIX_DEVICE_ID = (os.getenv("MATRIX_DEVICE_ID") or "").strip() or "redmine_bot"


def env_placeholder_hints() -> list[str]:
    """
    Значения как в .env.example — для предупреждения в логе при старте бота.
    Не вызывать до load_dotenv() в точке входа.
    """
    hints: list[str] = []
    hs = (os.getenv("MATRIX_HOMESERVER") or "").lower()
    uid = (os.getenv("MATRIX_USER_ID") or "").lower()
    tok = (os.getenv("MATRIX_ACCESS_TOKEN") or "").strip()
    ru = (os.getenv("REDMINE_URL") or "").lower()
    rk = (os.getenv("REDMINE_API_KEY") or "").strip()
    if "your-matrix-server.example.com" in hs:
        hints.append("MATRIX_HOMESERVER всё ещё как в .env.example")
    if "your-matrix-server.example.com" in uid:
        hints.append("MATRIX_USER_ID всё ещё как в .env.example")
    if tok == "your_access_token_here":
        hints.append("MATRIX_ACCESS_TOKEN не заменён (your_access_token_here)")
    if "your-redmine.example.com" in ru:
        hints.append("REDMINE_URL всё ещё как в .env.example")
    if rk == "your_api_key_here":
        hints.append("REDMINE_API_KEY не заменён (your_api_key_here)")
    return hints


# ═══════════════════════════════════════════════════════════════
# REDMINE
# ═══════════════════════════════════════════════════════════════

REDMINE_URL = os.getenv("REDMINE_URL")
REDMINE_API_KEY = os.getenv("REDMINE_API_KEY")

# ═══════════════════════════════════════════════════════════════
# ТАЙМЗОНА
# ═══════════════════════════════════════════════════════════════

BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
set_timezone(BOT_TIMEZONE)

# ═══════════════════════════════════════════════════════════════
# ИНТЕРВАЛЫ
# ═══════════════════════════════════════════════════════════════

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
REMINDER_AFTER = int(os.getenv("REMINDER_AFTER", "3600"))
GROUP_REPEAT_SECONDS = int(os.getenv("GROUP_REPEAT_SECONDS", "1800"))

# ═══════════════════════════════════════════════════════════════
# MATRIX — RETRY / BACKOFF
# ═══════════════════════════════════════════════════════════════

MATRIX_RETRY_MAX_ATTEMPTS = int(os.getenv("MATRIX_RETRY_MAX_ATTEMPTS", "3"))
MATRIX_RETRY_BASE_DELAY_SEC = float(os.getenv("MATRIX_RETRY_BASE_DELAY_SEC", "1.0"))

# ═══════════════════════════════════════════════════════════════
# BOT — LEASE / HEARTBEAT / CONFIG POLL
# ═══════════════════════════════════════════════════════════════

BOT_LEASE_TTL_SECONDS = int(os.getenv("BOT_LEASE_TTL_SECONDS", "300"))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "60"))
CONFIG_POLL_INTERVAL_SEC = int(os.getenv("CONFIG_POLL_INTERVAL_SEC", "30"))
COMMAND_POLL_INTERVAL_SEC = int(os.getenv("COMMAND_POLL_INTERVAL_SEC", "20"))

# ═══════════════════════════════════════════════════════════════
# СТАТУСЫ REDMINE и приоритеты — re-export из bot.logic (единственный источник)
# ═══════════════════════════════════════════════════════════════

from bot.logic import (  # noqa: E402, I001
    NOTIFICATION_TYPES,
    PRIORITY_EMERGENCY,
    PRIORITY_NAMES,
    STATUSES_TRANSFERRED,
    STATUS_INFO_PROVIDED,
    STATUS_NAMES,
    STATUS_NEW,
    STATUS_REOPENED,
    STATUS_RV,
    should_notify,
    validate_users,
)

__all__ = [
    # env / paths
    "BASE_DIR",
    "DATA_DIR",
    "LOG_FILE",
    "want_log_file",
    "log_file_max_bytes",
    "log_file_backup_count",
    "env_placeholder_hints",
    # matrix / redmine
    "MATRIX_HOMESERVER",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_USER_ID",
    "MATRIX_DEVICE_ID",
    "REDMINE_URL",
    "REDMINE_API_KEY",
    # tz / intervals
    "BOT_TIMEZONE",
    "CHECK_INTERVAL",
    "REMINDER_AFTER",
    "GROUP_REPEAT_SECONDS",
    # matrix retry
    "MATRIX_RETRY_MAX_ATTEMPTS",
    "MATRIX_RETRY_BASE_DELAY_SEC",
    # bot internals
    "BOT_LEASE_TTL_SECONDS",
    "HEARTBEAT_INTERVAL_SEC",
    "CONFIG_POLL_INTERVAL_SEC",
    "COMMAND_POLL_INTERVAL_SEC",
    # statuses (re-export from bot.logic)
    "STATUS_NEW",
    "STATUS_INFO_PROVIDED",
    "STATUS_REOPENED",
    "STATUS_RV",
    "STATUSES_TRANSFERRED",
    "STATUS_NAMES",
    "PRIORITY_NAMES",
    "PRIORITY_EMERGENCY",
    "NOTIFICATION_TYPES",
    # validation (re-export from bot.logic)
    "should_notify",
    "validate_users",
    # users
    "USERS",
    "STATUS_ROOM_MAP",
    "VERSION_ROOM_MAP",
    "ROOM_RED_OS_KEY",
    "ROOM_VIRT_KEY",
    # env check
    "validate_required_env",
]

# ═══════════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛИ (JSON из .env)
# ═══════════════════════════════════════════════════════════════


def _parse_json_env(var_name: str, default: str = "{}") -> dict | list:
    """Парсит JSON из переменной окружения. При ошибке — default."""
    raw = os.getenv(var_name, default)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"❌ Ошибка парсинга {var_name}: {e}")
        return json.loads(default)


USERS = _parse_json_env("USERS", "[]")
STATUS_ROOM_MAP = _parse_json_env("STATUS_ROOM_MAP", "{}")
VERSION_ROOM_MAP = _parse_json_env("VERSION_ROOM_MAP", "{}")

# ═══════════════════════════════════════════════════════════════
# РОУТИНГ — ключи для VERSION_ROOM_MAP
# ═══════════════════════════════════════════════════════════════

ROOM_RED_OS_KEY = "РЕД ОС"
ROOM_VIRT_KEY = "РЕД Виртуализация"

# ═══════════════════════════════════════════════════════════════
# ВАЛИДАЦИЯ
# ═══════════════════════════════════════════════════════════════


def validate_required_env() -> tuple[bool, list[str]]:
    """Проверяет наличие обязательных переменных окружения."""
    errors = []
    required = {
        "MATRIX_HOMESERVER": MATRIX_HOMESERVER,
        "MATRIX_ACCESS_TOKEN": MATRIX_ACCESS_TOKEN,
        "MATRIX_USER_ID": MATRIX_USER_ID,
        "REDMINE_URL": REDMINE_URL,
        "REDMINE_API_KEY": REDMINE_API_KEY,
    }
    for name, value in required.items():
        if not value:
            errors.append(f"Не задана переменная {name}")
    return len(errors) == 0, errors
