"""Периодическая подгрузка конфигурации из БД без рестарта процесса бота.

Отключение: BOT_HOT_RELOAD=0. Интервал: BOT_HOT_RELOAD_INTERVAL_SEC (по умолчанию 45).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.catalogs import BotCatalogs, load_catalogs
from database.load_config import fetch_runtime_config

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("redmine_bot")

JOB_POLL_ALL = "via_bot_poll_all"
JOB_POLL_UNASSIGNED = "via_bot_poll_unassigned_new"
JOB_DAILY_REPORT = "via_bot_daily_report"
JOB_HOT_RELOAD = "via_bot_config_hot_reload"


@dataclass
class EnvBaseline:
    """Значения из config.py (env) как fallback для cycle_int."""

    check_interval: int
    reminder_after: int
    group_repeat_seconds: int
    bot_lease_ttl: int
    bot_timezone: str
    matrix_device_id: str


@dataclass
class BotRuntimeSnapshot:
    fingerprint: str
    users: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    status_map: dict[str, str]
    version_map: dict[str, str]
    routes_config: dict[str, Any]
    catalogs: BotCatalogs
    check_interval: int
    reminder_after: int
    group_repeat_seconds: int
    bot_lease_ttl_seconds: int
    bot_timezone: str
    bot_tz: Any  # ZoneInfo
    matrix_device_id: str
    daily_report_enabled: bool
    daily_report_hour: int
    daily_report_minute: int


def _users_fingerprint(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in users:
        d = {k: v for k, v in u.items() if not str(k).startswith("_")}
        d["_has_rm_key"] = bool(u.get("_redmine_key_cipher"))
        out.append(d)
    return out


def _snapshot_fingerprint(snap: BotRuntimeSnapshot) -> str:
    cats = snap.catalogs
    payload = {
        "users": _users_fingerprint(snap.users),
        "groups": snap.groups,
        "status_map": sorted(snap.status_map.items()),
        "version_map": sorted(snap.version_map.items()),
        "routes": json.dumps(snap.routes_config, sort_keys=True, ensure_ascii=False, default=str),
        "cycle": sorted(snap.catalogs.cycle_settings.items()),
        "sid": sorted(cats.status_id_to_name.items()),
        "pid": sorted(cats.priority_id_to_name.items()),
        "nt": sorted(cats.notification_types.items()),
        "tnew": sorted(cats.trigger_new_ids),
        "tinfo": sorted(cats.trigger_info_provided_ids),
        "treo": sorted(cats.trigger_reopened_ids),
        "ttr": sorted(cats.trigger_transferred_ids),
        "closed": sorted(cats.closed_status_ids),
        "emerg_nm": sorted(cats.emergency_priority_names),
        "emerg_id": sorted(cats.emergency_priority_ids),
        "ci": snap.check_interval,
        "ra": snap.reminder_after,
        "gr": snap.group_repeat_seconds,
        "lease": snap.bot_lease_ttl_seconds,
        "tz": snap.bot_timezone,
        "md": snap.matrix_device_id,
        "de": snap.daily_report_enabled,
        "dh": snap.daily_report_hour,
        "dm": snap.daily_report_minute,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_hour(value: int) -> int:
    return max(0, min(23, int(value)))


def _safe_minute(value: int) -> int:
    return max(0, min(59, int(value)))


async def build_snapshot(session: AsyncSession, baseline: EnvBaseline) -> BotRuntimeSnapshot:
    from zoneinfo import ZoneInfo

    u, sm, vm, g, routes_cfg = await fetch_runtime_config(session)
    catalogs = await load_catalogs(session)

    ci = catalogs.cycle_int("CHECK_INTERVAL", baseline.check_interval)
    ra = catalogs.cycle_int("REMINDER_AFTER", baseline.reminder_after)
    gr = catalogs.cycle_int("GROUP_REPEAT_SECONDS", baseline.group_repeat_seconds)
    lease = max(
        15,
        min(catalogs.cycle_int("BOT_LEASE_TTL_SECONDS", baseline.bot_lease_ttl), 3600),
    )

    tz_name = (catalogs.cycle_settings.get("BOT_TIMEZONE") or "").strip() or baseline.bot_timezone
    bot_tz = ZoneInfo(tz_name)

    md = (catalogs.cycle_settings.get("MATRIX_DEVICE_ID") or "").strip()
    if not md:
        md = baseline.matrix_device_id

    de = str(catalogs.cycle_settings.get("DAILY_REPORT_ENABLED", "1")).lower() in (
        "1",
        "true",
        "on",
    )
    dh = _safe_hour(catalogs.cycle_int("DAILY_REPORT_HOUR", 9))
    dmm = _safe_minute(catalogs.cycle_int("DAILY_REPORT_MINUTE", 0))

    snap = BotRuntimeSnapshot(
        fingerprint="",
        users=u,
        groups=g,
        status_map=sm or {},
        version_map=vm or {},
        routes_config=routes_cfg or {},
        catalogs=catalogs,
        check_interval=ci,
        reminder_after=ra,
        group_repeat_seconds=gr,
        bot_lease_ttl_seconds=lease,
        bot_timezone=tz_name,
        bot_tz=bot_tz,
        matrix_device_id=md[:255] if md else baseline.matrix_device_id,
        daily_report_enabled=de,
        daily_report_hour=dh,
        daily_report_minute=dmm,
    )
    snap.fingerprint = _snapshot_fingerprint(snap)
    return snap


async def refresh_runtime_lists_from_db(session_factory: async_sessionmaker) -> None:
    """
    Перечитывает пользователей, группы и глобальные маршруты из БД в память процесса.

    Вызывается в начале каждого цикла check_all_users, чтобы удаление пользователей
    в админке отражалось без ожидания hot reload и даже если hot reload не смог достучаться до БД.
    """
    import bot.config_state as cs_mod
    import bot.main as main_mod

    try:
        async with session_factory() as session:
            u, sm, vm, g, routes_cfg = await fetch_runtime_config(session)
    except Exception as e:
        logger.warning("⚠ Список пользователей из БД не обновлён: %s", e)
        return

    main_mod.USERS = u
    main_mod.GROUPS = g
    main_mod.STATUS_ROOM_MAP = sm or {}
    main_mod.VERSION_ROOM_MAP = vm or {}
    cs_mod.ROUTING = routes_cfg or {}

    cs_mod.USERS.clear()
    cs_mod.USERS.extend(u)
    cs_mod.GROUPS.clear()
    cs_mod.GROUPS.extend(g)
    cs_mod.STATUS_ROOM_MAP.clear()
    cs_mod.STATUS_ROOM_MAP.update(sm or {})
    cs_mod.VERSION_ROOM_MAP.clear()
    cs_mod.VERSION_ROOM_MAP.update(vm or {})


def apply_snapshot_to_runtime(
    main_mod: Any,
    cs_mod: Any,
    snap: BotRuntimeSnapshot,
) -> None:
    """Обновляет bot.main и config_state."""
    from bot.config_state import (
        GROUPS as _SG,
    )
    from bot.config_state import (
        STATUS_ROOM_MAP as _SR,
    )
    from bot.config_state import (
        USERS as _SU,
    )
    from bot.config_state import (
        VERSION_ROOM_MAP as _SV,
    )

    main_mod.USERS = snap.users
    main_mod.GROUPS = snap.groups
    main_mod.STATUS_ROOM_MAP = snap.status_map
    main_mod.VERSION_ROOM_MAP = snap.version_map
    cs_mod.ROUTING = snap.routes_config

    _SU.clear()
    _SU.extend(snap.users)
    _SG[:] = snap.groups
    _SR.clear()
    _SR.update(snap.status_map)
    _SV.clear()
    _SV.update(snap.version_map)

    cs_mod.CATALOGS = snap.catalogs

    main_mod.CHECK_INTERVAL = snap.check_interval
    main_mod.REMINDER_AFTER = snap.reminder_after
    main_mod.GROUP_REPEAT_SECONDS = snap.group_repeat_seconds
    main_mod.BOT_LEASE_TTL_SECONDS = snap.bot_lease_ttl_seconds
    main_mod.BOT_TIMEZONE = snap.bot_timezone
    main_mod.BOT_TZ = snap.bot_tz
    main_mod.MATRIX_DEVICE_ID = snap.matrix_device_id


def reschedule_after_reload(
    scheduler: AsyncIOScheduler,
    snap: BotRuntimeSnapshot,
    reload_ctx: dict[str, Any],
) -> None:
    """Подстраивает интервалы и cron под новый снимок (APScheduler 3.x)."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    tz = snap.bot_tz
    check_interval = snap.check_interval

    for job_id in (JOB_POLL_ALL, JOB_POLL_UNASSIGNED):
        job = scheduler.get_job(job_id)
        if job is None:
            continue
        scheduler.reschedule_job(
            job_id, trigger=IntervalTrigger(seconds=check_interval, timezone=tz)
        )
        kw = dict(job.kwargs)
        kw["bot_lease_ttl"] = snap.bot_lease_ttl_seconds
        # Только check_all_users принимает check_interval; check_unassigned_new_issues — нет.
        if job_id == JOB_POLL_ALL:
            kw["check_interval"] = check_interval
        job.modify(kwargs=kw)

    daily_job = scheduler.get_job(JOB_DAILY_REPORT)
    if snap.daily_report_enabled:
        trigger = CronTrigger(
            hour=snap.daily_report_hour,
            minute=snap.daily_report_minute,
            timezone=tz,
        )
        if daily_job is None:
            from bot.scheduler import daily_report

            scheduler.add_job(
                daily_report,
                trigger,
                args=[reload_ctx["client"], reload_ctx["redmine"]],
                kwargs=reload_ctx["daily_kwargs"],
                id=JOB_DAILY_REPORT,
            )
        else:
            scheduler.reschedule_job(JOB_DAILY_REPORT, trigger=trigger)
    elif daily_job is not None:
        scheduler.remove_job(JOB_DAILY_REPORT)


def is_hot_reload_enabled() -> bool:
    v = (os.getenv("BOT_HOT_RELOAD") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def hot_reload_interval_sec() -> int:
    raw = (os.getenv("BOT_HOT_RELOAD_INTERVAL_SEC") or "").strip()
    if not raw:
        return 45
    try:
        return max(15, min(int(raw), 3600))
    except ValueError:
        return 45


async def run_hot_reload_once(
    *,
    session_factory: async_sessionmaker,
    baseline: EnvBaseline,
    main_mod: Any,
    scheduler: AsyncIOScheduler | None,
    reload_ctx: dict[str, Any] | None,
) -> None:
    if not is_hot_reload_enabled():
        return

    last_fp: str | None = getattr(main_mod, "_hot_reload_last_fp", None)

    try:
        async with session_factory() as session:
            snap = await build_snapshot(session, baseline)
    except Exception as e:
        logger.warning("hot_reload: снимок конфигурации не получен: %s", e)
        return

    if last_fp is None:
        main_mod._hot_reload_last_fp = snap.fingerprint
        return

    if snap.fingerprint == last_fp:
        return

    logger.info("♻ Hot reload: конфигурация из БД изменилась (poll/шаблоны/пользователи/интервалы)")

    import bot.config_state as cs_mod

    apply_snapshot_to_runtime(main_mod, cs_mod, snap)
    main_mod._hot_reload_last_fp = snap.fingerprint

    if scheduler is not None and reload_ctx is not None:
        try:
            reschedule_after_reload(scheduler, snap, reload_ctx)
        except Exception as e:
            logger.error("hot_reload: планировщик не обновлён: %s", e, exc_info=True)
