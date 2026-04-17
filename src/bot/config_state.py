"""Глобальное состояние конфигурации бота.

Заполняется из main.py при старте (загрузка из БД).
Используется processor.py и scheduler.py.
"""

from __future__ import annotations

# Пользователи бота (загружаются из БД)
USERS: list[dict] = []

# Группы бота (загружаются из БД)
GROUPS: list[dict] = []

# Маршрутизация: статус → Matrix room
STATUS_ROOM_MAP: dict[str, str] = {}

# Маршрутизация: версия → Matrix room (глобальный)
VERSION_ROOM_MAP: dict[str, str] = {}

# Справочники из БД (заполняется при старте)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.catalogs import BotCatalogs

CATALOGS: BotCatalogs | None = None
