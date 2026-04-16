"""Extended helpers for admin panel — всё что не влезло в helpers.py.

Содержит: timezone utils, secret/catalog loaders, group helpers,
audit/CRUD helpers, parsing utils, Matrix helpers, ops flash messages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, available_timezones

from cachetools import TTLCache as _TTLCache
from sqlalchemy import func, or_, select

from admin.crud_events_log import (
    actor_label_for_crud_log,
    format_crud_line,
    sanitize_audit_details,
    want_admin_audit_crud_db,
    want_admin_events_log_crud,
)
from admin.helpers import (
    _ROOT,
    CATALOG_NOTIFY_SECRET,
    CATALOG_VERSIONS_SECRET,
    GROUP_UNASSIGNED_DISPLAY,
    GROUP_UNASSIGNED_NAME,
    GROUP_USERS_FILTER_ALL_LABEL,
    SERVICE_TIMEZONE_FALLBACK,
    _now_utc,
)
from database.models import AppSecret, BotOpsAudit, BotUser, SupportGroup
from events_log_display import admin_events_log_timestamp_now
from security import SecurityError, decrypt_secret, encrypt_secret, load_master_key

if TYPE_CHECKING:
    from nio import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

# ── Public API (для from admin.helpers_ext import *) ─────────────────────────
__all__ = [
    "ADMIN_EXISTS_CACHE_TTL_SECONDS",
    "INTEGRATION_STATUS_CACHE_TTL_SECONDS",
    "MATRIX_DEFAULT_DEVICE_ID",
    "REQUIRED_SECRET_NAMES",
    "RUNTIME_STATUS_FILE",
    "SESSION_IDLE_TIMEOUT_SECONDS",
    "SHOW_DEV_TOKENS",
    "_LOGIN_RE",
    "_OPS_FLASH_WITH_DETAIL",
    "_admin_events_log_path",
    "_admin_events_log_scan_bytes",
    "_append_audit_file_line_local",
    "_append_ops_to_events_log_local",
    "_audit_op",
    "_build_room_id_async",
    "_catalog_key_from_label",
    "_dash_events_tail_line_count",
    "_dashboard_counts",
    "_default_notify_catalog",
    "_default_versions_catalog",
    "_get_matrix_client",
    "_get_matrix_domain_from_db",
    "_group_display_name",
    "_group_excluded_from_assignable_lists",
    "_groups_assignable",
    "_infer_crud_entity_id",
    "_integration_status",
    "_is_reserved_support_group",
    "_load_catalogs",
    "_load_secret_plain",
    "_matrix_bot_mxid",
    "_matrix_bot_mxid_from_db",
    "_matrix_domain",
    "_matrix_domain_from_db",
    "_maybe_log_admin_crud",
    "_normalize_notify",
    "_normalize_notify_catalog",
    "_normalize_service_timezone_name",
    "_normalize_versions",
    "_normalize_versions_catalog",
    "_normalized_group_filter_key",
    "_status_preset",
    "_ops_flash_message",
    "_parse_catalog_payload",
    "_parse_json_string_list",
    "_parse_notify",
    "_parse_status_keys_list",
    "_parse_work_days",
    "_parse_work_hours_range",
    "_persist_admin_crud_audit",
    "_read_events_log_scan",
    "_read_log_tail",
    "_room_localpart",
    "_runtime_status_from_file",
    "_standard_timezone_options",
    "_sync_matrix_client",
    "_timezone_labels",
    "_top_timezone_options",
    "_truncate_ops_detail",
    "_upsert_secret_plain",
    "_version_preset",
]

logger = logging.getLogger("admin")

# ── Constants ────────────────────────────────────────────────────────────────

RUNTIME_STATUS_FILE = os.getenv("BOT_RUNTIME_STATUS_FILE", "/app/data/runtime_status.json")
SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv("ADMIN_SESSION_IDLE_TIMEOUT", "1800"))
ADMIN_EXISTS_CACHE_TTL_SECONDS = int(os.getenv("ADMIN_EXISTS_CACHE_TTL_SECONDS", "20"))
INTEGRATION_STATUS_CACHE_TTL_SECONDS = int(os.getenv("INTEGRATION_STATUS_CACHE_TTL_SECONDS", "30"))

REQUIRED_SECRET_NAMES = [
    v.strip()
    for v in os.getenv(
        "REQUIRED_SECRET_NAMES",
        "REDMINE_URL,REDMINE_API_KEY,MATRIX_HOMESERVER,MATRIX_ACCESS_TOKEN,MATRIX_USER_ID",
    ).split(",")
    if v.strip()
]

MATRIX_DEFAULT_DEVICE_ID = (
    os.getenv("MATRIX_DEFAULT_DEVICE_ID") or "redmine_bot"
).strip() or "redmine_bot"
SHOW_DEV_TOKENS = os.getenv("SHOW_DEV_TOKENS", "0").strip().lower() in ("1", "true", "yes", "on")

_LOGIN_RE = re.compile(r"^[a-zA-Z0-9@._+-]{3,255}$")

# ── Timezone helpers ─────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _standard_timezone_options() -> list[str]:
    """IANA timezone list with RU priority zones first."""
    preferred = [
        "Europe/Moscow",
        "Asia/Ufa",
        "Asia/Yekaterinburg",
        "Asia/Omsk",
        "Asia/Krasnoyarsk",
        "Asia/Irkutsk",
        "Asia/Vladivostok",
    ]
    values = sorted(
        tz
        for tz in available_timezones()
        if "/" in tz and not tz.startswith(("Etc/", "posix/", "right/"))
    )
    ordered = [tz for tz in preferred if tz in values]
    preferred_set = set(ordered)
    ordered.extend([tz for tz in values if tz not in preferred_set])
    return ordered


@lru_cache(maxsize=1)
def _top_timezone_options() -> list[str]:
    """Frequently used timezones for the default compact select list."""
    preferred = [
        "Europe/Moscow",
        "Europe/Kaliningrad",
        "Europe/Samara",
        "Europe/Volgograd",
        "Europe/Astrakhan",
        "Europe/Ulyanovsk",
        "Europe/Kirov",
        "Europe/Simferopol",
        "Europe/Minsk",
        "Europe/Kyiv",
        "Europe/Riga",
        "Europe/Vilnius",
        "Europe/Tallinn",
        "Europe/Warsaw",
        "Europe/Berlin",
        "Europe/Paris",
        "Europe/London",
        "Europe/Madrid",
        "Europe/Rome",
        "Europe/Istanbul",
        "Asia/Yerevan",
        "Asia/Tbilisi",
        "Asia/Baku",
        "Asia/Almaty",
        "Asia/Tashkent",
        "Asia/Yekaterinburg",
        "Asia/Ufa",
        "Asia/Omsk",
        "Asia/Novosibirsk",
        "Asia/Krasnoyarsk",
        "Asia/Vladivostok",
    ]
    all_set = set(_standard_timezone_options())
    result = [tz for tz in preferred if tz in all_set]
    if len(result) < 30:
        for tz in _standard_timezone_options():
            if tz in result:
                continue
            result.append(tz)
            if len(result) >= 30:
                break
    return result[:30]


def _timezone_labels(options: list[str]) -> dict[str, str]:
    """Readable timezone labels with UTC offset and local time."""
    labels: dict[str, str] = {}
    for tz_name in options:
        try:
            now_local = datetime.now(ZoneInfo(tz_name))
            delta = now_local.utcoffset() or timedelta(0)
            total_minutes = int(delta.total_seconds() // 60)
            sign = "+" if total_minutes >= 0 else "-"
            abs_minutes = abs(total_minutes)
            hh = abs_minutes // 60
            mm = abs_minutes % 60
            labels[tz_name] = f"{tz_name} (UTC{sign}{hh:02d}:{mm:02d}, {now_local:%H:%M})"
        except Exception:
            labels[tz_name] = tz_name
    return labels


def _normalize_service_timezone_name(value: str) -> str:
    tz_name = (value or "").strip()
    if tz_name and tz_name in set(_standard_timezone_options()):
        return tz_name
    return SERVICE_TIMEZONE_FALLBACK


# ── Secret / Catalog loaders ─────────────────────────────────────────────────


async def _load_secret_plain(session: AsyncSession, name: str) -> str:
    """Load decrypted secret from DB."""
    q = await session.execute(select(AppSecret).where(AppSecret.name == name))
    row = q.scalar_one_or_none()
    if row is None:
        return ""
    key = load_master_key()
    try:
        return decrypt_secret(row.ciphertext, row.nonce, key)
    except SecurityError:
        logger.warning("secret_decrypt_failed name=%s", name)
        return ""


async def _upsert_secret_plain(session: AsyncSession, name: str, value: str) -> None:
    """Encrypt and upsert a secret."""
    key = load_master_key()
    enc = encrypt_secret(value, key=key)
    q = await session.execute(select(AppSecret).where(AppSecret.name == name))
    row = q.scalar_one_or_none()
    if row is None:
        session.add(
            AppSecret(
                name=name, ciphertext=enc.ciphertext, nonce=enc.nonce, key_version=enc.key_version
            )
        )
        return
    row.ciphertext = enc.ciphertext
    row.nonce = enc.nonce
    row.key_version = enc.key_version


def _default_notify_catalog() -> list[dict[str, str]]:
    return []


def _default_versions_catalog() -> list[str]:
    return []


def _catalog_key_from_label(label: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not base:
        base = "opt"
    key = base
    i = 2
    while key in used:
        key = f"{base}_{i}"
        i += 1
    return key


def _normalize_notify_catalog(data) -> list[dict[str, str]]:
    if not isinstance(data, list):
        return _default_notify_catalog()
    out: list[dict[str, str]] = []
    used: set[str] = set()
    for item in data:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            key = str(item.get("key") or "").strip().lower()
        else:
            label = str(item).strip()
            key = ""
        if not label:
            continue
        if not key:
            key = _catalog_key_from_label(label, used)
        if key in used:
            continue
        used.add(key)
        out.append({"key": key, "label": label})
    return out


def _normalize_versions_catalog(data) -> list[str]:
    if not isinstance(data, list):
        return _default_versions_catalog()
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


async def _load_catalogs(session: AsyncSession) -> tuple[list[dict[str, str]], list[str]]:
    raw_notify = await _load_secret_plain(session, CATALOG_NOTIFY_SECRET)
    raw_versions = await _load_secret_plain(session, CATALOG_VERSIONS_SECRET)
    if raw_notify:
        try:
            notify_catalog = _normalize_notify_catalog(json.loads(raw_notify))
        except json.JSONDecodeError:
            notify_catalog = _default_notify_catalog()
    else:
        notify_catalog = _default_notify_catalog()
    if raw_versions:
        try:
            versions_catalog = _normalize_versions_catalog(json.loads(raw_versions))
        except json.JSONDecodeError:
            versions_catalog = _default_versions_catalog()
    else:
        versions_catalog = _default_versions_catalog()
    return notify_catalog, versions_catalog


async def _load_statuses_catalog(session: AsyncSession) -> list[dict[str, str]]:
    """Загружает активные статусы из таблицы RedmineStatus."""
    from sqlalchemy import select

    from database.models import RedmineStatus

    result = await session.execute(
        select(RedmineStatus)
        .where(RedmineStatus.is_active == True)
        .order_by(RedmineStatus.id)
    )
    rows = result.scalars().all()
    return [
        {
            "id": str(r.redmine_status_id),
            "key": str(r.redmine_status_id),
            "label": r.name,
            "is_default": r.is_default,
        }
        for r in rows
    ]


async def _load_versions_catalog(session: AsyncSession) -> list[dict[str, str]]:
    """Загружает активные версии из таблицы RedmineVersion."""
    from sqlalchemy import select

    from database.models import RedmineVersion

    result = await session.execute(
        select(RedmineVersion)
        .where(RedmineVersion.is_active == True)
        .order_by(RedmineVersion.id)
    )
    rows = result.scalars().all()
    return [
        {
            "id": str(r.redmine_version_id),
            "key": str(r.redmine_version_id),
            "label": r.name,
            "is_default": r.is_default,
        }
        for r in rows
    ]


async def _load_priorities_catalog(session: AsyncSession) -> list[dict[str, str]]:
    """Загружает активные приоритеты из таблицы RedminePriority."""
    from sqlalchemy import select

    from database.models import RedminePriority

    result = await session.execute(
        select(RedminePriority)
        .where(RedminePriority.is_active == True)
        .order_by(RedminePriority.id)
    )
    rows = result.scalars().all()
    return [
        {
            "id": str(r.redmine_priority_id),
            "key": str(r.redmine_priority_id),
            "label": r.name,
            "is_default": r.is_default,
        }
        for r in rows
    ]


def _parse_catalog_payload(
    notify_raw: str, versions_raw: str
) -> tuple[list[dict[str, str]], list[str]]:
    if notify_raw:
        try:
            notify_catalog = _normalize_notify_catalog(json.loads(notify_raw))
        except json.JSONDecodeError:
            notify_catalog = _default_notify_catalog()
    else:
        notify_catalog = _default_notify_catalog()
    if versions_raw:
        try:
            versions_catalog = _normalize_versions_catalog(json.loads(versions_raw))
        except json.JSONDecodeError:
            versions_catalog = _default_versions_catalog()
    else:
        versions_catalog = _default_versions_catalog()
    return notify_catalog, versions_catalog


# ── Group helpers ────────────────────────────────────────────────────────────


def _normalized_group_filter_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name or "")
    compact_spaces = " ".join(normalized.replace("\u00a0", " ").split())
    return compact_spaces.strip().casefold()


def _group_excluded_from_assignable_lists(name: str | None) -> bool:
    if name is None:
        return False
    s = str(name).strip()
    if not s:
        return False
    if s == GROUP_UNASSIGNED_NAME:
        return True
    if _normalized_group_filter_key(s) == _normalized_group_filter_key(
        GROUP_USERS_FILTER_ALL_LABEL
    ):
        return True
    return False


def _groups_assignable(groups: list) -> list:
    return [
        g for g in groups if not _group_excluded_from_assignable_lists(getattr(g, "name", None))
    ]


def _is_reserved_support_group(row) -> bool:
    return row is not None and getattr(row, "name", None) == GROUP_UNASSIGNED_NAME


def _group_display_name(groups_by_id: dict, group_id: int | None) -> str:
    if group_id is None:
        return GROUP_UNASSIGNED_DISPLAY
    g = groups_by_id.get(group_id)
    if not g:
        return GROUP_UNASSIGNED_DISPLAY
    if g.name == GROUP_UNASSIGNED_NAME:
        return GROUP_UNASSIGNED_DISPLAY
    return g.name


# ── Audit / CRUD helpers ─────────────────────────────────────────────────────


def _append_audit_file_line_local(message: str) -> None:
    """Local audit file writer (overrides helpers version if ADMIN_AUDIT_LOG_PATH set)."""
    raw = (os.getenv("ADMIN_AUDIT_LOG_PATH") or "").strip()
    if raw.lower() in ("-", "none", "off", "false", "0"):
        return
    if not raw:
        path = Path(__file__).resolve().parents[2] / "data" / "admin_audit.log"
    else:
        p = Path(raw)
        path = p if p.is_absolute() else Path(__file__).resolve().parents[2] / p
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = admin_events_log_timestamp_now()
        line = f"{ts} [AUDIT] {(message or '').strip()}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.warning("Не удалось записать файл аудита (%s): %s", path, e)


def _admin_events_log_scan_bytes() -> int:
    raw = (os.getenv("ADMIN_EVENTS_LOG_SCAN_BYTES") or str(8 * 1024 * 1024)).strip()
    try:
        n = int(raw)
    except ValueError:
        return 8 * 1024 * 1024
    return max(64 * 1024, min(n, 64 * 1024 * 1024))


def _read_events_log_scan(path: Path, *, max_bytes: int) -> tuple[str, bool]:
    """Reads event log file fully or tail."""
    try:
        if not path.is_file():
            return (
                f"Файл лога не найден: {path}\n"
                "Проверьте LOG_TO_FILE у бота, том data/ и переменную ADMIN_EVENTS_LOG_PATH.",
            ), False
        size = path.stat().st_size
        with path.open("rb") as f:
            if size <= max_bytes:
                data = f.read()
                truncated = False
            else:
                f.seek(max(0, size - max_bytes))
                data = f.read()
                truncated = True
        text = data.decode("utf-8", errors="replace")
        if truncated:
            nl = text.find("\n")
            if nl != -1 and nl + 1 < len(text):
                text = text[nl + 1 :]
        return text, truncated
    except OSError as e:
        return f"Не удалось прочитать лог: {e}", False


def _admin_events_log_path() -> Path:

    raw = (os.getenv("ADMIN_EVENTS_LOG_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _ROOT / "data" / "bot.log"


def _read_log_tail(path: Path, *, max_lines: int = 400, max_bytes: int = 256_000) -> str:
    try:
        if not path.is_file():
            return (
                f"Файл лога не найден: {path}\n"
                "Проверьте LOG_TO_FILE у бота, том data/ и переменную ADMIN_EVENTS_LOG_PATH."
            )
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        return "\n".join(lines[-max_lines:]) if lines else ""
    except OSError as e:
        return f"Не удалось прочитать лог: {e}"


def _append_ops_to_events_log_local(message: str) -> None:
    """Appends [ADMIN] line to events log."""
    path: Path | None = None
    try:
        path = _admin_events_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = admin_events_log_timestamp_now()
        safe = (message or "").replace("\n", " ").replace("\r", " ").strip()[:800]
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{ts} [ADMIN] {safe}\n")
    except OSError as e:
        logger.warning(
            "Не удалось дописать строку [ADMIN] в лог событий %s: %s",
            path or "(unknown)",
            e,
            exc_info=True,
        )


def _dash_events_tail_line_count(*, max_lines: int = 400) -> int:
    path = _admin_events_log_path()
    if not path.is_file():
        return 0
    text = _read_log_tail(path, max_lines=max_lines)
    return sum(1 for line in text.splitlines() if line.strip())


async def _dashboard_counts(session: AsyncSession) -> dict[str, int]:
    user_count = int(
        (await session.execute(select(func.count()).select_from(BotUser))).scalar_one() or 0
    )
    group_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(SupportGroup)
                .where(SupportGroup.name != GROUP_UNASSIGNED_NAME)
            )
        ).scalar_one()
        or 0
    )
    users_ungrouped = int(
        (
            await session.execute(
                select(func.count())
                .select_from(BotUser)
                .where(
                    or_(
                        BotUser.group_id.is_(None),
                        BotUser.group_id.in_(
                            select(SupportGroup.id).where(
                                SupportGroup.name == GROUP_UNASSIGNED_NAME
                            )
                        ),
                    )
                )
            )
        ).scalar_one()
        or 0
    )
    return {
        "user_count": user_count,
        "group_count": group_count,
        "users_without_group": users_ungrouped,
        "events_tail_lines": _dash_events_tail_line_count(),
    }


async def _audit_op(
    session: AsyncSession,
    action: str,
    status: str,
    actor_login: str | None = None,
    detail: str | None = None,
) -> None:
    row = BotOpsAudit(
        actor_login=(actor_login or "").strip().lower() or None,
        action=action,
        status=status,
        detail=(detail or "")[:2000] or None,
    )
    session.add(row)
    d = ((detail or "").replace("\n", " "))[:1800]
    parts = [f"op={action}", f"status={status}"]
    al = (actor_login or "").strip()
    if al:
        parts.append(f"actor={al}")
    if d:
        parts.append(f"detail={d}")
    _append_audit_file_line_local(" ".join(parts))
    logger.info(
        json.dumps(
            {
                "level": "AUDIT",
                "action": action,
                "status": status,
                "actor": actor_login or "",
                "detail": detail or "",
                "ts": _now_utc().isoformat(),
            },
            ensure_ascii=False,
        )
    )


def _infer_crud_entity_id(entity_type: str, details: dict | None) -> int | None:
    """Numeric entity ID heuristic for bot_ops_audit indexing."""
    if not details:
        return None

    def gint(v: object) -> int | None:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            return None

    et = (entity_type or "").strip()
    if et == "bot_user":
        return gint(details.get("id"))
    if et == "group":
        return gint(details.get("id"))
    if et in ("group_version_route", "group_status_route"):
        return gint(details.get("group_id"))
    if et == "user_version_route":
        return gint(details.get("bot_user_id"))
    if et == "route/version_global":
        return gint(details.get("id"))
    if et == "self_settings":
        return gint(details.get("bot_user_id"))
    return None


async def _persist_admin_crud_audit(
    session: AsyncSession,
    request_actor,
    entity_type: str,
    crud_action: str,
    details: dict | None,
) -> None:
    actor_login = (getattr(request_actor, "login", None) or "").strip().lower() or None
    cleaned = sanitize_audit_details(details or {})
    entity_id = _infer_crud_entity_id(entity_type, details)
    et = (entity_type or "unknown")[:64]
    ca = (crud_action or "unknown")[:32]
    dj = json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
    if len(dj) > 2000:
        dj = dj[:1997] + "..."
    aud = f"ADMIN_CRUD entity={et} action={ca} actor={actor_login or ''}"
    if entity_id is not None:
        aud += f" entity_id={entity_id}"
    if dj:
        aud += f" details={dj}"
    _append_audit_file_line_local(aud)
    logger.info(
        json.dumps(
            {
                "level": "AUDIT",
                "action": "ADMIN_CRUD",
                "status": "ok",
                "actor": actor_login or "",
                "entity_type": et,
                "crud_action": ca,
                "entity_id": entity_id,
                "details": cleaned,
                "ts": _now_utc().isoformat(),
            },
            ensure_ascii=False,
        )
    )
    if not want_admin_audit_crud_db():
        return
    row = BotOpsAudit(
        actor_login=actor_login,
        action="ADMIN_CRUD",
        status="ok",
        detail=None,
        entity_type=et or None,
        entity_id=entity_id,
        crud_action=ca or None,
        details_json=cleaned if cleaned else None,
    )
    session.add(row)


async def _maybe_log_admin_crud(
    session: AsyncSession,
    request_actor,
    entity_type: str,
    action: str,
    details: dict | None = None,
) -> None:
    if want_admin_events_log_crud():
        actor = actor_label_for_crud_log(request_actor)
        line = format_crud_line(entity_type, action, actor, details)
        _append_ops_to_events_log_local(line)
    await _persist_admin_crud_audit(session, request_actor, entity_type, action, details)


# ── Ops flash messages ───────────────────────────────────────────────────────

_OPS_FLASH_MESSAGES: dict[str, str] = {
    "stop_ok": "Остановка бота выполнена. Если контейнер уже был выключен, состояние не менялось.",
    "stop_error": "Не удалось остановить бот. Проверьте DOCKER_HOST, docker-socket-proxy и имя сервиса.",
    "start_ok": "Бот запущен. Если он уже работал, ничего не изменилось.",
    "start_error": "Не удалось запустить бот. Проверьте Docker и настройки.",
    "restart_accepted": "Перезапуск бота запланирован (команда уходит в фоне).",
    "ops_commit_error": "Не удалось сохранить запись в журнал операций (БД).",
}

_OPS_FLASH_WITH_DETAIL = frozenset({"stop_error", "start_error", "ops_commit_error"})


def _truncate_ops_detail(s: str, max_len: int = 400) -> str:
    t = (s or "").replace("\n", " ").replace("\r", " ")
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t


def _ops_flash_message(ops: str | None, detail: str | None = None) -> str | None:
    if not ops:
        return None
    key = ops.strip()
    base = _OPS_FLASH_MESSAGES.get(key)
    if not base:
        return None
    d = (detail or "").strip()
    if d and key in _OPS_FLASH_WITH_DETAIL:
        return f"{base} Подробнее: {d}"
    return base


# ── Parsing helpers ──────────────────────────────────────────────────────────


def _parse_status_keys_list(raw: str) -> list[str]:
    parts = [p.strip() for p in (raw or "").replace("\n", ",").split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _parse_json_string_list(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _parse_notify(raw: str) -> list:
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else ["all"]
    except json.JSONDecodeError:
        return ["all"]


def _normalize_notify(values: list[str] | None, allowed_keys: list[str] | None = None) -> list[str]:
    vals = [v.strip() for v in (values or []) if v and v.strip()]
    if not vals:
        return ["all"]
    if "all" in vals:
        return ["all"]
    # NOTIFY_TYPE_KEYS — mutable list, заполняется из route файлов
    allowed_set = set(allowed_keys or [])
    allowed = [v for v in vals if v in allowed_set]
    return allowed or ["all"]


def _status_preset(notify: list | None) -> str:
    values = [str(x).strip() for x in (notify or []) if str(x).strip()]
    if not values or "all" in values:
        return "default"
    return "custom"


def _parse_work_days(raw: str) -> list[int] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else None
    except json.JSONDecodeError:
        return None


def _parse_work_hours_range(value: str) -> tuple[str, str]:
    if not value or "-" not in value:
        return "", ""
    start, end = value.split("-", 1)
    return start.strip(), end.strip()


def _normalize_versions(
    values: list[str] | None, allowed_values: list[str] | None = None
) -> list[str]:
    vals = [v.strip() for v in (values or []) if v and v.strip()]
    if not vals:
        return []
    allowed_set = set(allowed_values or [])
    if not allowed_set:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in vals:
        if v in seen or v not in allowed_set:
            continue
        seen.add(v)
        out.append(v)
    return out


def _version_preset(selected: list[str] | None, catalog: list[str] | None) -> str:
    selected_list = [str(x).strip() for x in (selected or []) if str(x).strip()]
    if not selected_list:
        return "all"
    return "custom"


# ── Matrix helpers ───────────────────────────────────────────────────────────


async def _get_matrix_client(session: AsyncSession) -> AsyncClient | None:
    """Creates Matrix client from DB secrets."""
    homeserver = await _load_secret_plain(session, "MATRIX_HOMESERVER")
    access_token = await _load_secret_plain(session, "MATRIX_ACCESS_TOKEN")
    bot_mxid = await _load_secret_plain(session, "MATRIX_USER_ID")

    if not all([homeserver, access_token, bot_mxid]):
        return None

    from nio import AsyncClient

    client = AsyncClient(homeserver, bot_mxid)
    client.access_token = access_token
    client.device_id = "redmine_bot_admin"
    client.restore_login(bot_mxid, "redmine_bot_admin", access_token)
    return client


async def _sync_matrix_client(client: AsyncClient, timeout: int = 10000) -> bool:
    """Syncs client. Returns True on success."""
    try:
        await client.sync(timeout=timeout)
        return True
    except Exception:
        return False


def _room_localpart(room_id: str) -> str:
    """Extracts localpart from room_id: !xxxxxx:server -> xxxxxx"""
    if not room_id:
        return ""
    if room_id.startswith("!") and ":" in room_id:
        return room_id[1:].split(":", 1)[0]
    return room_id


async def _build_room_id_async(localpart: str, session: AsyncSession) -> str:
    """Constructs full room_id from localpart + bot domain from DB."""
    mxid = await _load_secret_plain(session, "MATRIX_USER_ID")
    domain = mxid.split(":", 1)[1] if ":" in mxid else ""
    if not localpart or not domain:
        return localpart
    if localpart.startswith("!"):
        return localpart
    if localpart.startswith("@"):
        return f"{localpart.split(':', 1)[0]}:{domain}" if ":" not in localpart else localpart
    return f"!{localpart}:{domain}"


async def _matrix_bot_mxid_from_db(session: AsyncSession) -> str:
    return await _load_secret_plain(session, "MATRIX_USER_ID")


async def _matrix_domain_from_db(session: AsyncSession) -> str:
    mxid = await _matrix_bot_mxid_from_db(session)
    if ":" in mxid:
        return mxid.split(":", 1)[1]
    return ""


def _matrix_bot_mxid() -> str:
    return (os.getenv("MATRIX_USER_ID") or "").strip()


def _matrix_domain() -> str:
    mxid = _matrix_bot_mxid()
    if ":" in mxid:
        return mxid.split(":", 1)[1]
    return ""


async def _get_matrix_domain_from_db(session: AsyncSession) -> str:
    mxid = await _load_secret_plain(session, "MATRIX_USER_ID")
    if ":" in mxid:
        return mxid.split(":", 1)[1]
    return _matrix_domain()


# ── Integration status ───────────────────────────────────────────────────────

_integration_status_cache_ext = _TTLCache(maxsize=1, ttl=INTEGRATION_STATUS_CACHE_TTL_SECONDS)


async def _integration_status(session: AsyncSession, use_cache: bool = True) -> dict:
    if use_cache:
        cached = _integration_status_cache_ext.get("flag")
        if cached is not None:
            return cached
    rows = await session.execute(
        select(AppSecret.name).where(AppSecret.name.in_(REQUIRED_SECRET_NAMES))
    )
    names = {r[0] for r in rows.all()}
    missing = [name for name in REQUIRED_SECRET_NAMES if name not in names]
    status = {
        "configured": len(missing) == 0,
        "missing": missing,
    }
    _integration_status_cache_ext["flag"] = status
    return status


# ── Runtime status ───────────────────────────────────────────────────────────


def _runtime_status_from_file() -> dict:
    p = Path(RUNTIME_STATUS_FILE)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}
