# Источники правды: маршруты Matrix-комнат в runtime бота

Краткая карта **без изменения поведения** (фиксируем текущее состояние для следующего эпика унификации маршрутизации).

## Таблицы БД → `fetch_runtime_config`

| Таблица (ORM) | Что попадает в ответ [`fetch_runtime_config`](../src/database/load_config.py) |
|---------------|------------------|
| `SupportGroup` | Элементы списка `GROUPS`; комната группы участвует в fallback в [`get_matching_route`](../src/bot/routing.py). |
| `UserVersionRoute` | Входит в `user_orm_to_cfg` → поле пользователя `version_routes`. |
| `GroupVersionRoute` | То же для группы пользователя → `version_routes`. |
| `StatusRoomRoute` | Одновременно: плоская мапа `status_key → room_id` (**STATUS_ROOM_MAP**) и элементы списка `routes_config["status_routes"]`. |
| `VersionRoomRoute` | Одновременно: плоская мапа `version_key → room_id` (**VERSION_ROOM_MAP**) и элементы списка `routes_config["version_routes_global"]`. |

Итого: **одни и те же строки БД** дают два представления:

1. **`dict[str, str]`** — ключ комнаты/статуса (или версии) → один `room_id` (первая проекция по смыслу «один комнатный канон на ключ», без полей `priority`/`notify_on_assignment` на уровне dict).
2. **`routes_config`** — списки словарей с полным набором полей для скоринга в [`bot.routing`](../src/bot/routing.py).

## Потребители в рантайме

| Представление | Основные потребители |
|---------------|----------------------|
| `ROUTING` (`routes_config` из снимка) | [`get_matching_route`](../src/bot/routing.py) → журнал и напоминания. |
| `STATUS_ROOM_MAP` / `VERSION_ROOM_MAP` на модулях `bot.main`, [`config_state`](../src/bot/config_state.py) | Обертки [`get_extra_rooms_for_new` / `get_extra_rooms_for_rv`](../src/bot/main.py) → [`bot.logic`](../src/bot/logic.py); тесты ([`tests/test_bot.py`](../tests/test_bot.py)). |

## Следующий эпик (не текущая задача)

Объединение плоских карт и `routes_config` в одну структуру с адаптерами — отдельное изменение доменной модели; до него оба слоя остаются **намеренной** двойной проекцией из одной БД.

## Инвентаризация `config.py` (фаза E)

Рабочие потребители маршрутных данных — **`bot.main` / `bot.config_state`**. В [`src/config.py`](../src/config.py) legacy-имена `USERS` / `STATUS_ROOM_MAP` / `VERSION_ROOM_MAP` переведены в deprecation-shim через `__getattr__` (с `DeprecationWarning`) для мягкой совместимости со старыми импортами. Тесты маршрутизации патчат `bot.VERSION_ROOM_MAP` / `bot.STATUS_ROOM_MAP` ([`tests/test_bot.py`](../tests/test_bot.py)), а не `config.*`.
