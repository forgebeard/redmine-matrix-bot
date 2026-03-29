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
LOG_FILE = BASE_DIR / "bot.log"

# ═══════════════════════════════════════════════════════════════
# MATRIX
# ═══════════════════════════════════════════════════════════════

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER")
MATRIX_ACCESS_TOKEN = os.getenv("MATRIX_ACCESS_TOKEN")
MATRIX_USER_ID = os.getenv("MATRIX_USER_ID")
MATRIX_DEVICE_ID = os.getenv("MATRIX_DEVICE_ID")

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