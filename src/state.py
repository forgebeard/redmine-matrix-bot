"""
Управление state-файлами (вариант для кода из src/).

Чтение/запись JSON, атомарная запись, пути в data/state_<uid>_*.json.

Корневой bot.py дублирует load_json/save_json/state_file для автономного
запуска без обязательного пакетного импорта; поведение должно совпадать.
"""

import json
import logging
from pathlib import Path

from config import DATA_DIR, USERS

logger = logging.getLogger("redmine_bot")


def state_file(user_id: int, name: str) -> Path:
    """
    Путь к state-файлу пользователя.
    Пример: state_1972_sent.json, state_3254_journals.json
    """
    return DATA_DIR / f"state_{user_id}_{name}.json"


def load_json(filepath, default=None) -> dict:
    """Загрузка JSON из файла. При ошибке — возвращает default."""
    filepath = Path(filepath)
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"❌ Ошибка чтения {filepath.name}: {e}")
    return default if default is not None else {}


def save_json(filepath, data) -> bool:
    """
    Атомарная запись JSON (через tmp-файл, потом rename).
    Возвращает True при успехе, False при ошибке.
    """
    filepath = Path(filepath)
    tmp = filepath.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(filepath)
        return True
    except IOError as e:
        logger.error(f"❌ Ошибка записи {filepath.name}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def migrate_old_state():
    """
    Переносит старые state-файлы (до мультипользовательской версии)
    в новый формат state_<uid>_<name>.json для первого пользователя.
    """
    if not USERS:
        return

    first_uid = USERS[0]["redmine_id"]
    old_files = {
        "sent_issues.json": "sent",
        "reminders.json": "reminders",
        "overdue_issues.json": "overdue",
        "journals.json": "journals",
    }

    for old_name, new_name in old_files.items():
        old_path = DATA_DIR / old_name
        new_path = state_file(first_uid, new_name)
        if old_path.exists() and not new_path.exists():
            data = load_json(old_path)
            if data:
                save_json(new_path, data)
                logger.info(f"📦 Миграция: {old_name} → {new_path.name}")