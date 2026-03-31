"""
Конфигурация бота (модуль для импорта из src/).

Загрузка переменных из .env, парсинг JSON (USERS, маппинги комнат),
валидация USERS, справочники статусов/приоритетов Redmine.

Примечание: корневой bot.py дублирует часть настроек через свой load_dotenv —
исторически так сложилось; при рефакторинге можно оставить один источник.
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
DATA_DIR = BASE_DIR / "data"                       # State-файлы JSON


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

# ═══════════════════════════════════════════════════════════════
# СТАТУСЫ REDMINE
# ═══════════════════════════════════════════════════════════════

STATUS_NEW = "Новая"
STATUS_INFO_PROVIDED = "Информация предоставлена"
STATUS_REOPENED = "Открыто повторно"
STATUS_RV = "Передано в работу.РВ"

STATUSES_TRANSFERRED = {
    "Передано в работу.РВ",
    "Передано в работу.РА.Стд",
    "Передано в работу.РА.Пром",
    "Передано в работу.РБД",
    "Передано в работу.ВРМ",
}

# ═══════════════════════════════════════════════════════════════
# СПРАВОЧНИКИ (хардкод, потом можно загружать из API)
# ═══════════════════════════════════════════════════════════════

STATUS_NAMES = {
    "1": "Новая", "2": "В работе", "5": "Завершена",
    "6": "Отклонена", "8": "Ожидание", "12": "Запрос информации",
    "13": "Информация предоставлена", "17": "Ожидается решение",
    "18": "Открыто повторно", "22": "Передано в работу.РВ",
    "23": "Передано в работу.РБД", "25": "Передано в работу.РА.Стд",
    "26": "Передано в работу.РА.Пром", "27": "Проектирование",
    "28": "Передано в работу.ВРМ", "29": "Приостановлено",
    "30": "Передано на L2", "31": "Эскалация",
    "32": "Решен", "33": "Возвращен (L1)",
}

PRIORITY_NAMES = {
    "1": "4 (Низкий)", "2": "3 (Нормальный)",
    "3": "2 (Высокий)", "4": "1 (Аварийный)",
}

# Приоритет «Аварийный» — пробивает DND и выходные
PRIORITY_EMERGENCY = "1 (Аварийный)"

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

def validate_users(users: list) -> tuple[bool, list[str]]:
    """
    Проверяет структуру USERS.
    Возвращает (ok, errors). Вызывается при старте бота.
    """
    errors = []
    required_fields = ("redmine_id", "room")

    for i, u in enumerate(users):
        for field in required_fields:
            if field not in u:
                errors.append(f"USERS[{i}]: отсутствует поле '{field}'")
        if "redmine_id" in u and not isinstance(u["redmine_id"], int):
            errors.append(
                f"USERS[{i}]: 'redmine_id' должен быть int, "
                f"получено {type(u['redmine_id']).__name__}"
            )
        if "room" in u and (not isinstance(u["room"], str) or not u["room"].strip()):
            errors.append(f"USERS[{i}]: 'room' должен быть непустой строкой")
        if "notify" in u and not isinstance(u["notify"], list):
            errors.append(
                f"USERS[{i}]: 'notify' должен быть списком, "
                f"получено {type(u['notify']).__name__}"
            )

    return len(errors) == 0, errors


def should_notify(user_cfg: dict, notification_type: str) -> bool:
    """
    Проверяет подписку пользователя на тип уведомления.
    "all" — подписан на всё.
    """
    notify_list = user_cfg.get("notify", ["all"])
    return "all" in notify_list or notification_type in notify_list


def validate_required_env() -> tuple[bool, list[str]]:
    """Проверяет bootstrap-переменные окружения (без интеграционных секретов)."""
    errors = []
    has_db_url = bool((os.getenv("DATABASE_URL") or "").strip())
    has_pg_parts = all(
        bool((os.getenv(n) or "").strip())
        for n in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST", "POSTGRES_PORT")
    )
    if not has_db_url and not has_pg_parts:
        errors.append("Не задан DATABASE_URL и отсутствует полный набор POSTGRES_*")
    has_master_key_file = bool((os.getenv("APP_MASTER_KEY_FILE") or "").strip())
    has_master_key = bool((os.getenv("APP_MASTER_KEY") or "").strip())
    if not has_master_key_file and not has_master_key:
        errors.append("Не задан APP_MASTER_KEY_FILE/APP_MASTER_KEY для шифрования секретов")
    return len(errors) == 0, errors