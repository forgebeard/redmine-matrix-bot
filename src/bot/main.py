#!/usr/bin/env python3
"""
Redmine → Matrix бот уведомлений.

Entry point: загрузка конфига, инициализация компонентов, graceful shutdown.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import logging.handlers
import os
import signal
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redminelib import Redmine
from redminelib.exceptions import AuthError, BaseRedmineError, ForbiddenError

from bot.logic import (
    NOTIFICATION_TYPES,
    STATUS_INFO_PROVIDED,
    STATUS_NEW,
    STATUS_REOPENED,
    STATUS_RV,
    STATUSES_TRANSFERRED,
    describe_journal,
    detect_new_journals,
    detect_status_change,
    get_version_name,
    plural_days,
    resolve_field_value,
    should_notify,
    validate_users,
)
from config import (
    LOG_FILE,
    env_placeholder_hints,
    log_file_backup_count,
    log_file_max_bytes,
    want_log_file,
)
from logging_config import get_log_formatter, setup_json_logging

# Re-export для тестов (чистые функции из logic.py)
__all__ = [
    "STATUS_NEW",
    "STATUS_INFO_PROVIDED",
    "STATUS_REOPENED",
    "STATUS_RV",
    "STATUSES_TRANSFERRED",
    "NOTIFICATION_TYPES",
    "plural_days",
    "get_version_name",
    "should_notify",
    "validate_users",
    "detect_status_change",
    "detect_new_journals",
    "describe_journal",
    "resolve_field_value",
    # Wrapper'ы и sender (для тестов)
    "ensure_tz",
    "_cfg_for_room",
    "_group_room",
    "get_extra_rooms_for_new",
    "get_extra_rooms_for_rv",
    "_group_member_rooms",
    "send_matrix_message",
    "send_safe",
    "check_user_issues",
    "check_all_users",
    "daily_report",
    "cleanup_state_files",
]


# ── Wrapper'ы для тестов (делегируют в bot.logic) ───────────────────────────


def ensure_tz(dt: datetime) -> datetime:
    """Wrapper: гарантирует наличие таймзоны бота."""
    from bot.logic import ensure_tz as _raw

    return _raw(dt, BOT_TZ)


def _cfg_for_room(user_cfg: dict, room_id: str) -> dict:
    from bot.logic import _cfg_for_room as _raw

    return _raw(user_cfg, room_id)


def _group_room(user_cfg: dict) -> str:
    from bot.logic import _group_room as _raw

    return _raw(user_cfg)


def get_extra_rooms_for_new(issue, user_cfg: dict) -> set[str]:
    from bot.logic import get_extra_rooms_for_new as _raw

    return _raw(issue, user_cfg, VERSION_ROOM_MAP, USERS)


def get_extra_rooms_for_rv(issue, user_cfg: dict) -> set[str]:
    from bot.logic import get_extra_rooms_for_rv as _raw

    return _raw(issue, user_cfg, STATUS_ROOM_MAP, VERSION_ROOM_MAP, USERS)


def _group_member_rooms(user_cfg: dict) -> set[str]:
    from bot.logic import _group_member_rooms as _raw

    return _raw(user_cfg, USERS)


# ── Re-export sender и scheduler для тестов ──────────────────────────────────
# fmt: off
import bot.sender as _sender_mod  # noqa: E402, I001
from bot.processor import check_user_issues  # noqa: E402, I001
from bot.scheduler import check_all_users, cleanup_state_files, daily_report  # noqa: E402, I001
from bot.sender import send_matrix_message, send_safe  # noqa: E402, I001
# fmt: on

# ── Config (не-секретные) ───────────────────────────────────────────────────
from config import (  # noqa: E402, I001
    BOT_LEASE_TTL_SECONDS,
    BOT_TIMEZONE,
    CHECK_INTERVAL,
    CONFIG_POLL_INTERVAL_SEC,
    GROUP_REPEAT_SECONDS,
    MATRIX_DEVICE_ID as MATRIX_DEVICE_ID_ENV,
    REMINDER_AFTER,
)

MATRIX_DEVICE_ID = MATRIX_DEVICE_ID_ENV or "redmine_bot"
BOT_TZ = ZoneInfo(BOT_TIMEZONE)

# Lease
BOT_LEASE_TTL_SECONDS = max(15, min(BOT_LEASE_TTL_SECONDS, 3600))
_BOT_INSTANCE_ID_RAW = (os.getenv("BOT_INSTANCE_ID") or "").strip()
BOT_INSTANCE_ID_UUID = uuid.UUID(_BOT_INSTANCE_ID_RAW) if _BOT_INSTANCE_ID_RAW else uuid.uuid4()

# Пути
BASE_DIR = Path(__file__).resolve().parent
_ROOT = BASE_DIR.parent.parent


def data_dir() -> Path:
    """Каталог для bot.log (data/ рядом с bot.py)."""
    return BASE_DIR / "data"


def runtime_status_file() -> Path:
    raw = (os.getenv("BOT_RUNTIME_STATUS_FILE") or "").strip()
    if raw:
        return Path(raw)
    return data_dir() / "runtime_status.json"


# ── Глобальные переменные (заполняются в main()) ────────────────────────────

USERS: list[dict] = []
STATUS_ROOM_MAP: dict[str, str] = {}
VERSION_ROOM_MAP: dict[str, str] = {}

HOMESERVER: str = ""
ACCESS_TOKEN: str = ""
MATRIX_USER_ID: str = ""
REDMINE_URL: str = ""
REDMINE_KEY: str = ""

# Время последней успешной проверки для каждого пользователя
_last_check_time: dict[int, datetime] = {}

# ── Логирование ──────────────────────────────────────────────────────────────

logger = logging.getLogger("redmine_bot")
logger.setLevel(logging.INFO)
logging.getLogger("nio.responses").setLevel(logging.CRITICAL)
logging.getLogger("nio.crypto").setLevel(logging.WARNING)

setup_json_logging("redmine_bot")

if want_log_file():
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
        _log_path = LOG_FILE
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _fh = logging.handlers.RotatingFileHandler(
            _log_path,
            maxBytes=log_file_max_bytes(),
            backupCount=log_file_backup_count(),
            encoding="utf-8",
        )
        _fh.setFormatter(get_log_formatter())
        logger.addHandler(_fh)
    except PermissionError as e:
        logger.warning("Файловый лог недоступен (нет прав): %s", e)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            logger.warning("Файловый лог недоступен (errno=%s): %s", e.errno, e)
        else:
            raise

_ch = logging.StreamHandler()
_ch.setFormatter(get_log_formatter())
logger.addHandler(_ch)

# ── Утилиты ──────────────────────────────────────────────────────────────────


def now_tz():
    """Текущее время в таймзоне бота."""
    return datetime.now(tz=BOT_TZ)


def today_tz():
    """Сегодняшняя дата в таймзоне бота."""
    return now_tz().date()


def _log_redmine_list_error(uid: int, err: Exception, where: str) -> None:
    """Логирует сбой Redmine при issue.filter и т.п."""
    if isinstance(err, (AuthError, ForbiddenError)):
        logger.error("❌ Redmine доступ (%s, user %s): %s", where, uid, err)
    elif isinstance(err, BaseRedmineError):
        logger.error("❌ Redmine API (%s, user %s): %s", where, uid, err)
    else:
        logger.error("❌ Redmine (%s, user %s): %s", where, uid, err, exc_info=True)


# ── Entry point ──────────────────────────────────────────────────────────────


async def main() -> None:
    global \
        USERS, \
        STATUS_ROOM_MAP, \
        VERSION_ROOM_MAP, \
        HOMESERVER, \
        ACCESS_TOKEN, \
        MATRIX_USER_ID, \
        REDMINE_URL, \
        REDMINE_KEY

    logger.info("🚀 Бот запущен")

    for hint in env_placeholder_hints():
        logger.warning("⚠ Похоже на плейсхолдер из .env.example (замените в .env): %s", hint)

    # ── Ожидание готовности конфигурации из БД ──
    from sqlalchemy import text

    from database.session import get_session_factory
    from security import decrypt_secret, load_master_key

    poll_interval = CONFIG_POLL_INTERVAL_SEC
    session_factory = get_session_factory()
    _SECRET_NAMES = [
        "REDMINE_URL",
        "REDMINE_API_KEY",
        "MATRIX_HOMESERVER",
        "MATRIX_ACCESS_TOKEN",
        "MATRIX_USER_ID",
    ]

    while True:
        try:
            async with session_factory() as session:
                result = await session.execute(
                    text(
                        "SELECT name, ciphertext, nonce FROM app_secrets WHERE name = ANY(:names)"
                    ),
                    {"names": list(_SECRET_NAMES)},
                )
                secrets_map = {row.name: (row.ciphertext, row.nonce) for row in result}

            key = load_master_key()
            config: dict[str, str] = {}
            for name in _SECRET_NAMES:
                if name in secrets_map:
                    ct, nonce = secrets_map[name]
                    try:
                        config[name] = decrypt_secret(ct, nonce, key)
                    except Exception as exc:
                        logger.warning(
                            "⚠ Не удалось расшифровать секрет %s: %s",
                            name,
                            type(exc).__name__,
                        )
                        config[name] = ""
                else:
                    config[name] = ""

            missing = [name for name in _SECRET_NAMES if not config.get(name)]
            if not missing:
                HOMESERVER = config["MATRIX_HOMESERVER"]
                ACCESS_TOKEN = config["MATRIX_ACCESS_TOKEN"]
                MATRIX_USER_ID = config["MATRIX_USER_ID"]
                REDMINE_URL = config["REDMINE_URL"]
                REDMINE_KEY = config["REDMINE_API_KEY"]
                logger.info("✅ Конфиг загружен из БД")
                logger.info("🔑 [TEMP] REDMINE_URL = '%s'", config["REDMINE_URL"])
                break
            else:
                logger.warning(
                    "⏳ Конфиг не настроен (отсутствуют: %s/%s). Повтор через %d с...",
                    len(missing),
                    len(_SECRET_NAMES),
                    poll_interval,
                )
        except Exception as e:
            error_msg = str(e)
            if "relation" in error_msg and "does not exist" in error_msg:
                logger.warning("⏳ Ожидание инициализации БД (таблицы еще не созданы)...")
            else:
                logger.error(
                    "Ошибка загрузки конфига из БД (%s): %s",
                    type(e).__name__,
                    error_msg,
                )

        await asyncio.sleep(poll_interval)

    # ── Загрузка runtime-конфига (пользователи, маршруты) ──
    try:
        from database.load_config import fetch_runtime_config

        u, sm, vm = await fetch_runtime_config()
    except Exception as e:
        logger.error("❌ Не удалось загрузить конфиг из БД: %s", e, exc_info=True)
        return

    # Синхронизируем в config_state и main
    from bot.config_state import (
        STATUS_ROOM_MAP as _SR,
    )
    from bot.config_state import (
        USERS as _SU,
    )
    from bot.config_state import (
        VERSION_ROOM_MAP as _SV,
    )

    USERS = u
    STATUS_ROOM_MAP = sm or {}
    VERSION_ROOM_MAP = vm or {}
    _SU[:] = USERS
    _SR.clear()
    _SR.update(STATUS_ROOM_MAP)
    _SV.clear()
    _SV.update(VERSION_ROOM_MAP)

    logger.info("Конфиг из БД обновлён, пользователей: %s", len(USERS))

    # ── Инициализация sender template ──
    import bot.sender as _sender_mod
    from bot.sender import init_template

    _sender_mod.REDMINE_URL = REDMINE_URL
    init_template(_ROOT)

    # ── Инициализация processor config ──
    import bot.processor as _proc_mod

    _proc_mod.GROUP_REPEAT_SECONDS = GROUP_REPEAT_SECONDS
    _proc_mod.REMINDER_AFTER = REMINDER_AFTER

    # ── Подключение к Matrix ──
    from nio import AsyncClient

    client = AsyncClient(HOMESERVER, MATRIX_USER_ID)
    client.access_token = ACCESS_TOKEN
    client.device_id = MATRIX_DEVICE_ID or "redmine_bot"
    client.user_id = MATRIX_USER_ID
    logger.info("✅ Matrix: клиент создан для %s", MATRIX_USER_ID)

    # ── Первичная синхронизация Matrix (нужна для поиска DM-комнат) ──
    logger.info("📡 Matrix: первичная синхронизация...")
    try:
        sync_resp = await client.sync(timeout=30000, full_state=True)
        logger.info(
            "✅ Matrix sync: %d комнат загружено",
            len(client.rooms),
        )
    except Exception as e:
        logger.warning("⚠ Matrix sync не удался (DM-резолв может не работать): %s", e)

        # ── Pre-warm DM-комнат ──────────────────────────────────────────────────
    from bot.sender import prewarm_dm_rooms

    all_mxids = []
    for u_cfg in USERS:
        room = (u_cfg.get("room") or "").strip()
        if room:
            all_mxids.append(room)
        # group_room тоже может быть MXID
        gr = (u_cfg.get("group_room") or "").strip()
        if gr:
            all_mxids.append(gr)

    if all_mxids:
        await prewarm_dm_rooms(client, all_mxids)

    # ── Подключение к Redmine ──
    redmine = Redmine(REDMINE_URL, key=REDMINE_KEY)
    try:
        user = redmine.user.get("current")
        logger.info("✅ Redmine: %s %s", user.firstname, user.lastname)
    except Exception as e:
        logger.error("❌ Redmine подключение: %s", e)
        await client.close()
        return

    # ── Импорт функций для scheduler ──
    from bot.heartbeat import start_heartbeat_task
    from bot.processor import check_user_issues
    from bot.scheduler import (
        check_all_users,
        cleanup_state_files,
        daily_report,
        retry_dlq_notifications,
    )

    def _redmine_client_for_user(redmine_inst, user_cfg):
        from bot.sender import REDMINE_URL as _RU

        ciph = user_cfg.get("_redmine_key_cipher")
        nonce = user_cfg.get("_redmine_key_nonce")
        if not ciph or not nonce:
            return redmine_inst
        try:
            from security import decrypt_secret, load_master_key

            api_key = decrypt_secret(ciph, nonce, load_master_key())
            return Redmine(_RU, key=api_key)
        except Exception as e:
            logger.error(
                "Персональный ключ Redmine недоступен (user redmine_id=%s): %s",
                user_cfg.get("redmine_id"),
                type(e).__name__,
            )
            return redmine_inst

    # ── Планировщик ──
    scheduler = AsyncIOScheduler(timezone=BOT_TZ)

    scheduler.add_job(
        check_all_users,
        "interval",
        seconds=CHECK_INTERVAL,
        args=[client, redmine],
        kwargs={
            "now_tz": now_tz,
            "check_interval": CHECK_INTERVAL,
            "runtime_status_file": runtime_status_file(),
            "bot_instance_id": BOT_INSTANCE_ID_UUID,
            "bot_lease_ttl": BOT_LEASE_TTL_SECONDS,
            "redmine_client_for_user": _redmine_client_for_user,
            "check_user_issues_fn": check_user_issues,
            "last_check_time": _last_check_time,
            "max_concurrent": 5,
        },
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
        next_run_time=datetime.now(tz=BOT_TZ),
    )

    scheduler.add_job(
        daily_report,
        "cron",
        hour=9,
        minute=0,
        args=[client, redmine],
        kwargs={
            "now_tz": now_tz,
            "today_tz": today_tz,
            "redmine_client_for_user": _redmine_client_for_user,
            "redmine_url": REDMINE_URL,
        },
    )

    scheduler.add_job(
        cleanup_state_files,
        "cron",
        hour=3,
        minute=0,
        args=[redmine],
        kwargs={
            "now_tz": now_tz,
            "redmine_client_for_user": _redmine_client_for_user,
        },
    )

    scheduler.add_job(
        retry_dlq_notifications,
        "interval",
        seconds=120,
        args=[client],
        kwargs={"now_tz": now_tz},
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        "✅ Планировщик: каждые %dс, таймзона %s, пользователей: %d",
        CHECK_INTERVAL,
        BOT_TZ,
        len(USERS),
    )

    # ── Heartbeat ──
    admin_url = os.getenv("ADMIN_URL", "http://admin:8080")
    start_heartbeat_task(admin_url)

    # ── Graceful shutdown ──
    stop_event = asyncio.Event()

    def _handle_signal(sig: int, _frame) -> None:
        sig_name = signal.Signals(sig).name
        logger.info("📥 Получен сигнал %s — завершение работы...", sig_name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        logger.info("💤 Бот работает, проверки по расписанию...")
        await stop_event.wait()
    finally:
        logger.info("👋 Бот остановлен — завершение работы...")
        scheduler.shutdown(wait=True)
        await client.close()
        logger.info("✅ Бот завершил работу корректно")


if __name__ == "__main__":
    asyncio.run(main())

# ── Post-import инициализация (после того как все переменные определены) ─────
_sender_mod.REDMINE_URL = REDMINE_URL
