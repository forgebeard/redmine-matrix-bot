#!/usr/bin/env python3
"""
Redmine → Matrix бот уведомлений.

Мониторит задачи нескольких пользователей в Redmine и шлёт уведомления
в Matrix-комнаты по настраиваемым правилам роутинга.

Типы уведомлений:
  - new            — новая задача (статус «Новая»)
  - info           — статус «Информация предоставлена»
  - reminder       — напоминание по «Информация предоставлена» (каждый час)
  - status_change  — смена статуса задачи
  - issue_updated  — комментарий или изменение полей в задаче
  - reopened       — задача открыта повторно
  - overdue        — просроченная задача (ежедневно)

Роутинг в доп. комнаты:
  - Версионные комнаты: по подстроке в названии версии
  - Статусные комнаты:  по точному совпадению статуса
  - Командная комната:  новые задачи определённого проекта

Конфигурация — через Postgres (админка заполняет `bot_users` и маппинги роутинга).
"""

import asyncio
import errno
import json
import re
import logging
import logging.handlers
import os
import sys
import uuid
import time  # FIX-4: метрика времени цикла
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
from utils import safe_html
from matrix_send import room_send_with_retry, MAX_RETRIES
from config import (
    LOG_FILE,
    env_placeholder_hints,
    log_file_backup_count,
    log_file_max_bytes,
    want_log_file,
)
from preferences import can_notify
from redminelib.exceptions import AuthError, BaseRedmineError, ForbiddenError

from dotenv import load_dotenv
from redminelib import Redmine
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
# НАСТРОЙКИ ИЗ .env (не-секретные)
# ═══════════════════════════════════════════════════════════════════════════

# --- Matrix (не-секретные) ---
MATRIX_DEVICE_ID = (os.getenv("MATRIX_DEVICE_ID") or "").strip() or "redmine_bot"

# --- Redmine ---
# REDMINE_URL и REDMINE_API_KEY загружаются из БД (app_secrets)

# --- Таймзона ---
BOT_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))

# --- Настройки пользователей / роутинга ---
# Clean-code режим: всё это хранится в Postgres и загружается в `main()`.
USERS = []
STATUS_ROOM_MAP = {}
VERSION_ROOM_MAP = {}

# ═══════════════════════════════════════════════════════════════════════════
# КОНСТАНТЫ
# ═══════════════════════════════════════════════════════════════════════════

# Пути к файлам
BASE_DIR = Path(__file__).resolve().parent


def data_dir() -> Path:
    """
    Каталог для bot.log (data/ рядом с bot.py).

    Функция, а не константа: в тестах подменяют bot.BASE_DIR — путь остаётся согласованным.
    """
    return BASE_DIR / "data"


def runtime_status_file() -> Path:
    raw = (os.getenv("BOT_RUNTIME_STATUS_FILE") or "").strip()
    if raw:
        return Path(raw)
    return data_dir() / "runtime_status.json"


# Интервал проверки Redmine (секунды); переопределение: CHECK_INTERVAL в .env
def _parse_check_interval() -> int:
    raw = os.getenv("CHECK_INTERVAL", "90").strip()
    try:
        v = int(raw)
    except ValueError:
        return 90
    return max(15, min(v, 86400))


CHECK_INTERVAL = _parse_check_interval()

# --- DB state (Postgres) ---
def _parse_bool(raw: str, default: bool = False) -> bool:
    v = (raw or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


BOT_LEASE_TTL_SECONDS = int(os.getenv("BOT_LEASE_TTL_SECONDS", "300").strip() or "300")
BOT_LEASE_TTL_SECONDS = max(15, min(BOT_LEASE_TTL_SECONDS, 3600))

# Идентификатор инстанса бота для lease (если не задан — генерируем при старте).
_BOT_INSTANCE_ID_RAW = (os.getenv("BOT_INSTANCE_ID") or "").strip()
BOT_INSTANCE_ID_UUID = uuid.UUID(_BOT_INSTANCE_ID_RAW) if _BOT_INSTANCE_ID_RAW else uuid.uuid4()

# Через сколько секунд напоминать о «Информация предоставлена»
REMINDER_AFTER = 3600
GROUP_REPEAT_SECONDS = int(os.getenv("GROUP_REPEAT_SECONDS", "1800").strip() or "1800")

# Кэш master key для расшифровки персональных ключей Redmine
_BOT_MASTER_KEY: bytes | None = None

# --- Статусы Redmine ---
STATUS_NEW           = "Новая"
STATUS_INFO_PROVIDED = "Информация предоставлена"
STATUS_REOPENED      = "Открыто повторно"
STATUS_RV            = "Передано в работу.РВ"

# Статусы «Передано в работу.*» — задачи с этими статусами НЕ дублируются
# в комнату РЕД ОС (только в специализированные комнаты)
STATUSES_TRANSFERRED = {
    "Передано в работу.РВ",
    "Передано в работу.РА.Стд",
    "Передано в работу.РА.Пром",
    "Передано в работу.РБД",
    "Передано в работу.ВРМ",
}

# Имена секретов, загружаемых из app_secrets
_SECRET_NAMES = [
    "REDMINE_URL",
    "REDMINE_API_KEY",
    "MATRIX_HOMESERVER",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_USER_ID",
]

# Глобальные переменные конфигурации (заполняются в main() из БД)
HOMESERVER: str = ""
ACCESS_TOKEN: str = ""
MATRIX_USER_ID: str = ""
REDMINE_URL: str = ""
REDMINE_KEY: str = ""

# ═══════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("redmine_bot")
logger.setLevel(logging.INFO)

# Файл с ротацией (LOG_MAX_BYTES × LOG_BACKUP_COUNT), если LOG_TO_FILE не отключён; иначе только stdout
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
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(_fh)
    except PermissionError as e:
        logger.warning("Файловый лог недоступен (нет прав): %s", e)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            logger.warning("Файловый лог недоступен (errno=%s): %s", e.errno, e)
        else:
            raise

# Консоль
_ch = logging.StreamHandler()
_ch.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(_ch)

# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════


def plural_days(n):
    """Склонение слова 'день': 1 день, 2 дня, 5 дней."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def now_tz():
    """Текущее время в таймзоне бота."""
    return datetime.now(tz=BOT_TZ)


def ensure_tz(dt: datetime) -> datetime:
    """Гарантирует наличие таймзоны у datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BOT_TZ)
    return dt


def today_tz():
    """Сегодняшняя дата в таймзоне бота."""
    return now_tz().date()


def get_version_name(issue):
    """Получает название версии задачи (или None)."""
    try:
        return issue.fixed_version.name
    except Exception:
        return None


def should_notify(user_cfg, notification_type):
    """
    Проверяет, подписан ли пользователь на данный тип уведомлений.
    "all" — подписан на всё.
    """
    notify_list = user_cfg.get("notify", ["all"])
    return "all" in notify_list or notification_type in notify_list


def _issue_priority_name(issue):
    try:
        return issue.priority.name
    except Exception:
        return ""


def _log_redmine_list_error(uid: int, err: Exception, where: str) -> None:
    """Логирует сбой Redmine при issue.filter и т.п.; неожиданные — с traceback."""
    if isinstance(err, (AuthError, ForbiddenError)):
        logger.error("❌ Redmine доступ (%s, user %s): %s", where, uid, err)
    elif isinstance(err, BaseRedmineError):
        logger.error("❌ Redmine API (%s, user %s): %s", where, uid, err)
    else:
        logger.error("❌ Redmine (%s, user %s): %s", where, uid, err, exc_info=True)


# FIX-4: валидация конфигурации пользователей
def validate_users(users):
    """
    Проверяет, что у каждого пользователя есть обязательные поля.
    Возвращает (ok: bool, errors: list[str]).
    """
    errors = []
    required_fields = ("redmine_id", "room")
    for i, u in enumerate(users):
        for field in required_fields:
            if field not in u:
                errors.append(f"USERS[{i}]: отсутствует обязательное поле '{field}'")
        # redmine_id должен быть числом
        if "redmine_id" in u and not isinstance(u["redmine_id"], int):
            errors.append(f"USERS[{i}]: 'redmine_id' должен быть int, получено {type(u['redmine_id']).__name__}")
        # room должна быть непустой строкой
        if "room" in u and (not isinstance(u["room"], str) or not u["room"].strip()):
            errors.append(f"USERS[{i}]: 'room' должен быть непустой строкой")
        # notify — если указан, должен быть списком
        if "notify" in u and not isinstance(u["notify"], list):
            errors.append(f"USERS[{i}]: 'notify' должен быть списком, получено {type(u['notify']).__name__}")
    return len(errors) == 0, errors


# ═══════════════════════════════════════════════════════════════════════════
# РОУТИНГ: какие доп. комнаты получают уведомление
# ═══════════════════════════════════════════════════════════════════════════

# Совместимость с прежней глобальной картой: задача без версии → комната ключа «РЕД ОС», если задан.
_LEGACY_VERSION_FALLBACK_KEY = "РЕД ОС"


def _extra_rooms_for_issue_version(issue, user_cfg: dict) -> set[str]:
    """
    Доп. комнаты по названию версии задачи в Redmine.
    Совпадение: подстрока version_key (без учёта регистра) входит в имя версии.
    Учитываются маршруты пользователя/группы (version_routes) и глобальная VERSION_ROOM_MAP.
    """
    rooms: set[str] = set()
    version_name = get_version_name(issue) or ""
    if not version_name.strip():
        r = (VERSION_ROOM_MAP.get(_LEGACY_VERSION_FALLBACK_KEY) or "").strip()
        return {r} if r else set()
    vn = version_name.lower()
    for spec in user_cfg.get("version_routes") or []:
        key = (spec.get("key") or "").strip()
        rid = (spec.get("room") or "").strip()
        if key and rid and key.lower() in vn:
            rooms.add(rid)
    for key, room in (VERSION_ROOM_MAP or {}).items():
        r = (room or "").strip()
        if not r:
            continue
        k = (key or "").strip()
        if k and k.lower() in vn:
            rooms.add(r)
    return rooms


def get_extra_rooms_for_new(issue, user_cfg: dict) -> set[str]:
    """Доп. комнаты для НОВОЙ задачи (статус «Новая») — по версии и глобальным маршрутам."""
    return _extra_rooms_for_issue_version(issue, user_cfg)


def get_extra_rooms_for_rv(issue, user_cfg: dict) -> set[str]:
    """Доп. комнаты для статуса «Передано в работу.РВ»: комната РВ из STATUS_ROOM_MAP + по версии."""
    rooms: set[str] = set()
    rv_room = STATUS_ROOM_MAP.get(STATUS_RV)
    if rv_room:
        rooms.add(rv_room)
    rooms |= _extra_rooms_for_issue_version(issue, user_cfg)
    return rooms


def _group_member_rooms(user_cfg: dict) -> set[str]:
    """Личные комнаты участников той же группы."""
    gid = user_cfg.get("group_id")
    if gid is None:
        return set()
    out: set[str] = set()
    for u in USERS:
        if u.get("group_id") != gid:
            continue
        r = (u.get("room") or "").strip()
        if r:
            out.add(r)
    return out


def _group_room(user_cfg: dict) -> str:
    return (user_cfg.get("group_room") or "").strip()


def _cfg_for_room(user_cfg: dict, room_id: str) -> dict:
    """
    Для Matrix-комнаты группы применяются типы уведомлений и расписание группы
    (из group_delivery), а не личные настройки пользователя.
    """
    target = (room_id or "").strip()
    gr = _group_room(user_cfg)
    if not target or not gr or target != gr:
        return user_cfg
    gd = user_cfg.get("group_delivery")
    if not isinstance(gd, dict):
        return user_cfg
    merged = dict(user_cfg)
    merged["notify"] = gd.get("notify") if isinstance(gd.get("notify"), list) else ["all"]
    wh = gd.get("work_hours")
    if wh:
        merged["work_hours"] = wh
    else:
        merged.pop("work_hours", None)
    wd = gd.get("work_days")
    if wd is not None:
        merged["work_days"] = wd
    else:
        merged.pop("work_days", None)
    merged["dnd"] = bool(gd.get("dnd"))
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# MATRIX: ОТПРАВКА СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════════════

NOTIFICATION_TYPES = {
    "new":           ("🆕", "Новая задача"),
    "info":          ("✅", "Информация предоставлена"),
    "reminder":      ("⏰", "Напоминание"),
    "overdue":       ("⚠️", "Просроченная задача"),
    "status_change": ("🔄", "Смена статуса"),
    "issue_updated": ("📝", "Задача обновлена"),
    "reopened":      ("🔁", "Открыто повторно"),
}


async def send_matrix_message(client, issue, room_id, notification_type="info", extra_text=""):
    """
    Формирует и отправляет HTML-сообщение в Matrix-комнату.
    FIX-1: проверяет ответ сервера — кидает исключение при ошибке.
    """
    issue_url = f"{REDMINE_URL}/issues/{issue.id}"
    emoji, title = NOTIFICATION_TYPES.get(notification_type, ("🔔", "Обратите внимание"))

    # Текст просрочки
    overdue_text = ""
    if notification_type == "overdue" and issue.due_date:
        days = (today_tz() - issue.due_date).days
        overdue_text = f" (просрочено на {plural_days(days)})"

    # Версия
    version = get_version_name(issue)
    version_line = f"<br/>Версия: {safe_html(version)}" if version else ""

    # Срок
    due_line = ""
    if issue.due_date:
        due_line = f"<br/>📅 Срок: {issue.due_date}{overdue_text}"

    subj = safe_html(issue.subject)
    st = safe_html(issue.status.name)
    pr = safe_html(issue.priority.name)

    # HTML — всё внутри <blockquote> для рамки
    html_body = (
        f"<blockquote>"
        f"<strong>{emoji} {title}</strong><br/>"
        f"<br/>"
        f'<a href="{issue_url}">#{issue.id}</a> — {subj}<br/>'
        f"<br/>"
        f"Статус: <strong>{st}</strong><br/>"
        f"Приоритет: {pr}"
        f"{version_line}"
        f"{due_line}"
    )
    if extra_text:
        html_body += f"<br/><br/>{extra_text}"
    html_body += (
        f"<br/><br/>"
        f'🔗 <a href="{issue_url}">Открыть задачу</a>'
        f"</blockquote>"
    )

    # Плоский текст (fallback)
    plain_body = (
        f"{emoji} {title} #{issue.id}: {issue.subject} "
        f"| Статус: {issue.status.name}"
    )

    content = {
        "msgtype": "m.text",
        "body": plain_body,
        "format": "org.matrix.custom.html",
        "formatted_body": html_body,
    }

    await room_send_with_retry(client, room_id, content)

    logger.info(f"📨 #{issue.id} → {room_id[:20]}... ({notification_type})")


async def send_safe(client, issue, user_cfg, room_id, notification_type, extra_text=""):
    """
    Обёртка send_matrix_message: DND/рабочие часы (can_notify), затем перехват ошибок Matrix.
    """
    cfg = _cfg_for_room(user_cfg, room_id)
    if not can_notify(cfg, priority=_issue_priority_name(issue)):
        logger.debug(
            "Пропуск уведомления (время/DND): user %s, #%s, %s",
            user_cfg.get("redmine_id"),
            issue.id,
            notification_type,
        )
        return
    try:
        await send_matrix_message(client, issue, room_id, notification_type, extra_text)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки #{issue.id} → {room_id[:20]}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# ДЕТЕКТОРЫ ИЗМЕНЕНИЙ
# ═══════════════════════════════════════════════════════════════════════════


def detect_status_change(issue, sent):
    """
    Сравнивает текущий статус задачи с сохранённым.
    Возвращает старый статус если изменился, иначе None.
    """
    issue_id = str(issue.id)
    if issue_id not in sent:
        return None
    old_status = sent[issue_id].get("status")
    if old_status and old_status != issue.status.name:
        return old_status
    return None


def detect_new_journals(issue, journals_state):
    """
    Находит новые записи в журнале задачи (комментарии, изменения полей).
    Returns: (new_journals, max_journal_id)
    """
    issue_id = str(issue.id)
    last_known_id = journals_state.get(issue_id, {}).get("last_journal_id", 0)

    try:
        all_journals = list(issue.journals)
    except Exception:
        return [], 0

    if not all_journals:
        return [], 0

    max_id = max(j.id for j in all_journals)
    new_journals = [j for j in all_journals if j.id > last_known_id]
    return new_journals, max_id


# Имена статусов по ID
STATUS_NAMES = {
    "1": "Новая",
    "2": "В работе",
    "5": "Завершена",
    "6": "Отклонена",
    "8": "Ожидание",
    "12": "Запрос информации",
    "13": "Информация предоставлена",
    "17": "Ожидается решение",
    "18": "Открыто повторно",
    "22": "Передано в работу.РВ",
    "23": "Передано в работу.РБД",
    "25": "Передано в работу.РА.Стд",
    "26": "Передано в работу.РА.Пром",
    "27": "Проектирование",
    "28": "Передано в работу.ВРМ",
    "29": "Приостановлено",
    "30": "Передано на L2",
    "31": "Эскалация",
    "32": "Решен",
    "33": "Возвращен (L1)",
}

# Имена приоритетов по ID
PRIORITY_NAMES = {
    "1": "4 (Низкий)",
    "2": "3 (Нормальный)",
    "3": "2 (Высокий)",
    "4": "1 (Аварийный)",
}

# Поля, для которых значения — ID из справочников
ID_FIELD_RESOLVERS = {
    "status_id": STATUS_NAMES,
    "priority_id": PRIORITY_NAMES,
}

# Маппинг технических имён полей → человекочитаемые
# FIX-4: теперь реально используется в describe_journal
FIELD_NAMES = {
    "status_id": "Статус",
    "assigned_to_id": "Назначена",
    "priority_id": "Приоритет",
    "done_ratio": "Готовность",
    "due_date": "Срок",
    "subject": "Тема",
    "description": None,  # Слишком длинное — пропускаем
    "tracker_id": "Трекер",
    "fixed_version_id": "Версия",
    "project_id": "Проект",
    "category_id": "Категория",
    "parent_id": "Родительская",
    "start_date": "Дата начала",
    "estimated_hours": "Оценка часов",
}

# Поля, которые всегда скрываем (кастомные поля вида "42")
HIDDEN_FIELDS_PATTERN = re.compile(r"^\d+$")


def resolve_field_value(field_name, value):
    """
    Переводит ID в человекочитаемое имя для известных полей.
    Например: status_id "13" → "Информация предоставлена".
    """
    if value is None or value == "":
        return "—"
    resolver = ID_FIELD_RESOLVERS.get(field_name)
    if resolver:
        return resolver.get(str(value), str(value))
    return str(value)


def describe_journal(journal, skip_status=False):
    """
    Описывает одну запись журнала в человекочитаемом виде.
    FIX-4: теперь показывает ВСЕ значимые поля (приоритет, назначение и т.д.),
    а не только status_id.
    """
    parts = []

    # Комментарий
    if journal.notes:
        try:
            parts.append(f"💬 Комментарий от {journal.user.name}")
        except Exception:
            parts.append("💬 Новый комментарий")

    # Изменения полей
    try:
        for detail in journal.details:
            prop = detail.get("name", detail.get("property", "?"))

            # Скрываем числовые кастомные поля (id вида "42")
            if HIDDEN_FIELDS_PATTERN.match(prop):
                continue

            # Пропуск статуса если уже отправлен в блоке status_change
            if prop == "status_id" and skip_status:
                continue

            # Получаем человекочитаемое название поля
            field_label = FIELD_NAMES.get(prop)
            if field_label is None:
                continue  # Неизвестное поле или description — пропускаем

            old_val = resolve_field_value(prop, detail.get("old_value"))
            new_val = resolve_field_value(prop, detail.get("new_value"))
            parts.append(f"{field_label}: {old_val} → {new_val}")
    except Exception:
        pass

    return "; ".join(parts) if parts else None


# ═══════════════════════════════════════════════════════════════════════════
# ОСНОВНАЯ ЛОГИКА: ПРОВЕРКА ЗАДАЧ ОДНОГО ПОЛЬЗОВАТЕЛЯ
# ═══════════════════════════════════════════════════════════════════════════


async def check_user_issues(client, redmine, user_cfg, db_session):
    """
    Проверяет все открытые задачи одного пользователя.
    Определяет что изменилось и рассылает уведомления.
    """
    uid  = user_cfg["redmine_id"]
    room = user_cfg["room"]

    # --- Загружаем задачи из Redmine ---
    try:
        issues = list(redmine.issue.filter(
            assigned_to_id=uid, status_id="open", include=["journals"]
        ))
    except Exception as e:
        _log_redmine_list_error(uid, e, "загрузка задач")
        return

    logger.info(f"👤 User {uid}: {len(issues)} задач")

    # --- Загружаем state (Postgres) ---
    from database.state_repo import load_user_issue_state

    sent, reminders, overdue_n, journals = await load_user_issue_state(db_session, uid)

    # Флаги и наборы для upsert в DB
    sent_ch = rem_ch = over_ch = jour_ch = False
    changed_sent: set[str] = set()
    changed_reminders: set[str] = set()
    changed_overdue: set[str] = set()
    changed_journals: set[str] = set()

    now   = now_tz()
    today = now.date()

    for issue in issues:
        iid = str(issue.id)

        try:
            # ══════════════════════════════════════════════════════
            # 1. СМЕНА СТАТУСА
            # ══════════════════════════════════════════════════════
            old_status = detect_status_change(issue, sent)
            if old_status:
                if should_notify(user_cfg, "status_change"):
                    extra = (
                        f"Статус: <strong>{safe_html(old_status)}</strong> "
                        f"→ <strong>{safe_html(issue.status.name)}</strong>"
                    )
                    await send_safe(client, issue, user_cfg, room, "status_change", extra_text=extra)
                sent[iid]["status"] = issue.status.name
                changed_sent.add(iid)
                sent_ch = True

            # ══════════════════════════════════════════════════════
            # 2. НОВАЯ ЗАДАЧА (статус «Новая»)
            # ══════════════════════════════════════════════════════
            if issue.status.name == STATUS_NEW and iid not in sent:
                if should_notify(user_cfg, "new"):
                    await send_safe(client, issue, user_cfg, room, "new")
                    for personal_room in _group_member_rooms(user_cfg):
                        if personal_room != room:
                            await send_safe(client, issue, user_cfg, personal_room, "new")
                    group_room = _group_room(user_cfg)
                    if group_room and should_notify(_cfg_for_room(user_cfg, group_room), "new"):
                        await send_safe(client, issue, user_cfg, group_room, "new")
                    for extra_room in get_extra_rooms_for_new(issue, user_cfg):
                        await send_safe(client, issue, user_cfg, extra_room, "new")
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": STATUS_NEW,
                    "group_last_notified_at": now.isoformat(),
                }
                changed_sent.add(iid)
                sent_ch = True

            # ══════════════════════════════════════════════════════
            # 3. ПЕРЕДАНО В РАБОТУ.РВ
            # ══════════════════════════════════════════════════════
            elif issue.status.name == STATUS_RV and iid not in sent:
                if should_notify(user_cfg, "new"):
                    await send_safe(client, issue, user_cfg, room, "new")
                    for personal_room in _group_member_rooms(user_cfg):
                        if personal_room != room:
                            await send_safe(client, issue, user_cfg, personal_room, "new")
                    group_room = _group_room(user_cfg)
                    if group_room and should_notify(_cfg_for_room(user_cfg, group_room), "new"):
                        await send_safe(client, issue, user_cfg, group_room, "new")
                    for extra_room in get_extra_rooms_for_rv(issue, user_cfg):
                        await send_safe(client, issue, user_cfg, extra_room, "new")
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": STATUS_RV,
                    "group_last_notified_at": now.isoformat(),
                }
                changed_sent.add(iid)
                sent_ch = True
            elif issue.status.name in (STATUS_NEW, STATUS_RV) and iid in sent:
                group_room = _group_room(user_cfg)
                if group_room:
                    last_group = sent.get(iid, {}).get("group_last_notified_at")
                    if last_group:
                        elapsed_group = (now - ensure_tz(datetime.fromisoformat(last_group))).total_seconds()
                    else:
                        elapsed_group = GROUP_REPEAT_SECONDS + 1
                    if elapsed_group >= GROUP_REPEAT_SECONDS and should_notify(
                        _cfg_for_room(user_cfg, group_room), "new"
                    ):
                        await send_safe(client, issue, user_cfg, group_room, "new")
                        sent[iid]["group_last_notified_at"] = now.isoformat()
                        changed_sent.add(iid)
                        sent_ch = True

            # ══════════════════════════════════════════════════════
            # 4. ИНФОРМАЦИЯ ПРЕДОСТАВЛЕНА
            # ══════════════════════════════════════════════════════
            elif issue.status.name == STATUS_INFO_PROVIDED:
                if iid not in sent:
                    if should_notify(user_cfg, "info"):
                        await send_safe(client, issue, user_cfg, room, "info")
                    sent[iid] = {"notified_at": now.isoformat(), "status": STATUS_INFO_PROVIDED}
                    changed_sent.add(iid)
                    sent_ch = True
                else:
                    # Напоминание каждый час
                    if should_notify(user_cfg, "reminder"):
                        last_rem = reminders.get(iid, {}).get("last_reminder")
                        if last_rem:
                            time_since = (now - ensure_tz(datetime.fromisoformat(last_rem))).total_seconds()
                        else:
                            notified_at = ensure_tz(datetime.fromisoformat(sent[iid]["notified_at"]))
                            time_since = (now - notified_at).total_seconds()

                        if time_since >= REMINDER_AFTER:
                            await send_safe(client, issue, user_cfg, room, "reminder")
                            reminders[iid] = {"last_reminder": now.isoformat()}
                            changed_reminders.add(iid)
                            rem_ch = True

            # ══════════════════════════════════════════════════════
            # 5. ОТКРЫТО ПОВТОРНО
            # ══════════════════════════════════════════════════════
            elif issue.status.name == STATUS_REOPENED and iid not in sent:
                if should_notify(user_cfg, "reopened"):
                    await send_safe(client, issue, user_cfg, room, "reopened")
                sent[iid] = {"notified_at": now.isoformat(), "status": STATUS_REOPENED}
                changed_sent.add(iid)
                sent_ch = True

            # ══════════════════════════════════════════════════════
            # 6. ПРОЧИЕ СТАТУСЫ — первое обнаружение (тихо)
            # ══════════════════════════════════════════════════════
            elif iid not in sent:
                sent[iid] = {"notified_at": now.isoformat(), "status": issue.status.name}
                changed_sent.add(iid)
                sent_ch = True

                        # ══════════════════════════════════════════════════════
            # 7. ПРОСРОЧЕННЫЕ ЗАДАЧИ
            # ══════════════════════════════════════════════════════
            # FIX-2: сравнение по дате, а не по timedelta.days
            # Было:  (now - ensure_tz(datetime.fromisoformat(last_n))).days >= 1
            # Стало: .date() < today — надёжно работает даже на границе суток
            if issue.due_date and issue.due_date < today:
                if should_notify(user_cfg, "overdue"):
                    last_n = overdue_n.get(iid, {}).get("last_notified")
                    if not last_n or ensure_tz(datetime.fromisoformat(last_n)).date() < today:
                        await send_safe(client, issue, user_cfg, room, "overdue")
                        overdue_n[iid] = {"last_notified": now.isoformat()}
                        changed_overdue.add(iid)
                        over_ch = True

            # ══════════════════════════════════════════════════════
            # 8. ЖУРНАЛЫ: КОММЕНТАРИИ И ИЗМЕНЕНИЯ ПОЛЕЙ
            # ══════════════════════════════════════════════════════
            new_jrnls, max_id = detect_new_journals(issue, journals)

            # Защита от спама старыми журналами:
            # если задачи НЕТ в journals state — запоминаем max_id БЕЗ отправки
            if iid not in journals:
                if max_id > 0:
                    journals[iid] = {"last_journal_id": max_id}
                    changed_journals.add(iid)
                    jour_ch = True
                    logger.debug(f"📝 #{iid}: инициализация journal_id={max_id} (пропуск)")
            elif new_jrnls and iid in sent and should_notify(user_cfg, "issue_updated"):
                _skip_st = old_status is not None
                descs = [d for d in (describe_journal(j, skip_status=_skip_st) for j in new_jrnls) if d]
                if descs:
                    tail = descs[-5:]
                    combined = "<br/>".join(safe_html(d) for d in tail)
                    if len(descs) > 5:
                        combined = f"<em>...и ещё {len(descs) - 5}</em><br/>" + combined
                    await send_safe(client, issue, user_cfg, room, "issue_updated", extra_text=combined)

                if max_id > journals.get(iid, {}).get("last_journal_id", 0):
                    journals[iid] = {"last_journal_id": max_id}
                    changed_journals.add(iid)
                    jour_ch = True
            else:
                if max_id > journals.get(iid, {}).get("last_journal_id", 0):
                    journals[iid] = {"last_journal_id": max_id}
                    changed_journals.add(iid)
                    jour_ch = True

        except Exception as e:
            logger.error(f"❌ Ошибка обработки #{issue.id} (user {uid}): {e}", exc_info=True)
            continue

    # --- Сохраняем state (Postgres) ---
    from database.state_repo import upsert_user_issue_state

    issue_ids_changed = (
        changed_sent
        | changed_reminders
        | changed_overdue
        | changed_journals
    )
    if issue_ids_changed:
        await upsert_user_issue_state(
            db_session,
            uid,
            issue_ids_changed,
            sent,
            reminders,
            overdue_n,
            journals,
        )


# ═══════════════════════════════════════════════════════════════════════════
# REDMINE: глобальный ключ + опционально персональный (из Postgres, AES-GCM)
# ═══════════════════════════════════════════════════════════════════════════


def _get_bot_master_key() -> bytes:
    global _BOT_MASTER_KEY
    if _BOT_MASTER_KEY is None:
        from security import load_master_key

        _BOT_MASTER_KEY = load_master_key()
    return _BOT_MASTER_KEY


def redmine_client_for_user(global_redmine: Redmine, user_cfg: dict) -> Redmine:
    """Персональный API-ключ из bot_users, иначе глобальный REDMINE_API_KEY."""
    ciph = user_cfg.get("_redmine_key_cipher")
    nonce = user_cfg.get("_redmine_key_nonce")
    if not ciph or not nonce:
        return global_redmine
    try:
        from security import decrypt_secret

        api_key = decrypt_secret(ciph, nonce, _get_bot_master_key())
        return Redmine(REDMINE_URL, key=api_key)
    except Exception as e:
        logger.error(
            "Персональный ключ Redmine недоступен (user redmine_id=%s): %s",
            user_cfg.get("redmine_id"),
            type(e).__name__,
        )
        return global_redmine


async def reload_runtime_users_from_db() -> None:
    """Перечитать USERS и маршруты из Postgres (после онбординга в Matrix)."""
    global USERS, STATUS_ROOM_MAP, VERSION_ROOM_MAP
    from database.load_config import fetch_runtime_config

    u, sm, vm = await fetch_runtime_config()
    USERS = u
    STATUS_ROOM_MAP = sm or {}
    VERSION_ROOM_MAP = vm or {}
    logger.info("Конфиг из БД обновлён, пользователей: %s", len(USERS))


# ═══════════════════════════════════════════════════════════════════════════
# ПЛАНИРОВЩИК: ПЕРИОДИЧЕСКИЕ ЗАДАЧИ
# ═══════════════════════════════════════════════════════════════════════════


async def check_all_users(client, redmine):
    """Проверка задач ВСЕХ пользователей. Вызывается по таймеру."""
    # FIX-4: метрика времени цикла
    start = time.monotonic()
    logger.info(f"🔍 Проверка в {now_tz().strftime('%H:%M:%S')}...")

    # DB-only: lease + upsert в `bot_issue_state`.
    from database.session import get_session_factory
    from database.state_repo import try_acquire_user_lease

    session_factory = get_session_factory()
    lease_owner_id = BOT_INSTANCE_ID_UUID
    lease_ttl = BOT_LEASE_TTL_SECONDS
    error_count = 0

    async with session_factory() as session:
        for user_cfg in USERS:
            uid = user_cfg.get("redmine_id")
            lease_until = datetime.now(timezone.utc) + timedelta(seconds=lease_ttl)
            try:
                acquired = await try_acquire_user_lease(
                    session,
                    uid,
                    lease_owner_id=lease_owner_id,
                    lease_until=lease_until,
                )
                if not acquired:
                    continue

                await session.commit()
                rm_user = redmine_client_for_user(redmine, user_cfg)
                await check_user_issues(client, rm_user, user_cfg, db_session=session)
                await session.commit()
            except Exception as e:
                logger.error("❌ DB-state цикл проверки user %s: %s", uid, e, exc_info=True)
                error_count += 1
                try:
                    await session.rollback()
                except Exception:
                    pass

    elapsed = time.monotonic() - start
    try:
        status_path = runtime_status_file()
        status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_cycle_at": now_tz().isoformat(),
            "last_cycle_duration_s": round(elapsed, 3),
            "error_count": int(error_count),
        }
        status_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("Не удалось обновить runtime_status.json", exc_info=True)
    logger.info(f"✅ Проверка завершена за {elapsed:.1f}с")
    if elapsed > CHECK_INTERVAL * 0.8:
        logger.warning(
            "⚠️ Цикл (%dс) > 0.8×интервала (%dс). Увеличьте CHECK_INTERVAL в .env или "
            "сократите число пользователей/API на цикл. Для SLA «до нескольких минут» это допустимо.",
            int(elapsed),
            CHECK_INTERVAL,
        )


async def daily_report(client, redmine):
    """
    Утренний отчёт (09:00) — каждому пользователю с notify=all.
    Показывает: кол-во задач, «Инфо предоставлена», просроченные.
    """
    logger.info("📊 Утренний отчёт...")

    for user_cfg in USERS:
        if not should_notify(user_cfg, "all"):
            continue
        if not can_notify(user_cfg, priority="", dt=now_tz()):
            logger.debug("Утренний отчёт: пропуск (время/DND), user %s", user_cfg.get("redmine_id"))
            continue

        uid  = user_cfg["redmine_id"]
        room = user_cfg["room"]
        rm_user = redmine_client_for_user(redmine, user_cfg)

        try:
            issues = list(rm_user.issue.filter(assigned_to_id=uid, status_id="open"))
        except Exception as e:
            _log_redmine_list_error(uid, e, "утренний отчёт")
            continue

        today = today_tz()
        info_provided = [i for i in issues if i.status.name == STATUS_INFO_PROVIDED]
        overdue = sorted(
            [i for i in issues if i.due_date and i.due_date < today],
            key=lambda i: i.due_date
        )

        html = f"<h3>📅 Отчёт на {today.strftime('%d.%m.%Y')}</h3>"
        html += f"<p><strong>Открытых задач:</strong> {len(issues)}</p>"
        html += f"<p><strong>«{STATUS_INFO_PROVIDED}»:</strong> {len(info_provided)}</p>"

        if info_provided:
            html += "<ul>"
            for i in info_provided[:10]:
                html += (
                    f'<li><a href="{REDMINE_URL}/issues/{i.id}">#{i.id}</a> '
                    f"— {safe_html(i.subject)}</li>"
                )
            html += "</ul>"
            if len(info_provided) > 10:
                html += f"<p><em>...и ещё {len(info_provided) - 10}</em></p>"

        html += f"<p><strong>Просроченных:</strong> {len(overdue)}</p>"
        if overdue:
            html += "<ul>"
            for i in overdue[:10]:
                days = (today - i.due_date).days
                html += (
                    f'<li><a href="{REDMINE_URL}/issues/{i.id}">#{i.id}</a> '
                    f"— {safe_html(i.subject)} ({plural_days(days)})</li>"
                )
            html += "</ul>"

        plain = f"Отчёт {today.strftime('%d.%m.%Y')}: {len(issues)} задач, {len(overdue)} просрочено"

        try:
            await room_send_with_retry(client, room, {
                "msgtype": "m.text", "body": plain,
                "format": "org.matrix.custom.html", "formatted_body": html,
            })
            logger.info(f"📊 Отчёт user {uid}: {len(issues)} задач")
        except Exception as e:
            logger.error(f"❌ Отправка отчёта user {uid}: {e}")


async def cleanup_state_files(redmine):
    """
    Очистка state в Postgres для закрытых задач (03:00).
    Удаляет записи о задачах, которых больше нет в открытых.
    """
    from database.session import get_session_factory
    from database.state_repo import delete_state_rows_not_in_open

    logger.info("🧹 Очистка state в Postgres для закрытых задач (03:00)...")
    session_factory = get_session_factory()

    async with session_factory() as session:
        for user_cfg in USERS:
            uid = user_cfg["redmine_id"]
            rm_user = redmine_client_for_user(redmine, user_cfg)
            try:
                open_issues = list(
                    rm_user.issue.filter(assigned_to_id=uid, status_id="open")
                )
            except Exception as e:
                _log_redmine_list_error(uid, e, "очистка state (db)")
                continue

            open_ids = {str(i.id) for i in open_issues}
            try:
                await delete_state_rows_not_in_open(session, uid, open_ids)
            except Exception as e:
                logger.error("❌ DB cleanup user %s: %s", uid, e, exc_info=True)

        await session.commit()

    logger.info("🧹 Очистка state в Postgres завершена")


# ═══════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    global USERS, STATUS_ROOM_MAP, VERSION_ROOM_MAP, HOMESERVER, ACCESS_TOKEN, MATRIX_USER_ID, REDMINE_URL, REDMINE_KEY

    logger.info("🚀 Бот запущен")

    for hint in env_placeholder_hints():
        logger.warning("⚠ Похоже на плейсхолдер из .env.example (замените в .env): %s", hint)

    # --- Ожидание готовности конфигурации из БД ---
    from database.session import get_session_factory
    from security import decrypt_secret, load_master_key
    from sqlalchemy import text

    poll_interval = 30  # секунд между попытками
    session_factory = get_session_factory()

    while True:
        try:
            async with session_factory() as session:
                result = await session.execute(
                    text(
                        "SELECT name, ciphertext, nonce FROM app_secrets "
                        "WHERE name = ANY(:names)"
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
                    except Exception:
                        logger.warning("⚠ Не удалось расшифровать секрет %s", name)
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
                break
            else:
                logger.warning(
                    "⏳ Конфиг не настроен (отсутствуют: %s). Повтор через %d с...",
                    ", ".join(missing),
                    poll_interval,
                )
        except Exception as e:
            error_msg = str(e)
            # Проверяем, не связана ли ошибка с отсутствием таблиц (БД еще инициализируется)
            if "relation" in error_msg and "does not exist" in error_msg:
                logger.warning("⏳ Ожидание инициализации БД (таблицы еще не созданы)...")
            else:
                logger.error("Ошибка загрузки конфига: %s", e)

        await asyncio.sleep(poll_interval)

    # Бот всегда стартует в DB-only режиме: конфиг берём из Postgres.
    try:
        from database.load_config import fetch_runtime_config

        u, sm, vm = await fetch_runtime_config()
    except Exception as e:
        logger.error("❌ Не удалось загрузить конфиг из БД: %s", e, exc_info=True)
        return

    # --- Подключение к Matrix ---
    from nio import AsyncClient
    client = AsyncClient(HOMESERVER, MATRIX_USER_ID)
    client.access_token = ACCESS_TOKEN
    client.device_id = MATRIX_DEVICE_ID or "redmine_bot"
    client.user_id = MATRIX_USER_ID
    logger.info(f"✅ Matrix: клиент создан для {MATRIX_USER_ID}")

    # --- Подключение к Redmine ---
    redmine = Redmine(REDMINE_URL, key=REDMINE_KEY)
    try:
        user = redmine.user.get("current")
        logger.info(f"✅ Redmine: {user.firstname} {user.lastname}")
    except Exception as e:
        logger.error(f"❌ Redmine подключение: {e}")
        await client.close()
        return

    # --- Планировщик ---
    scheduler = AsyncIOScheduler(timezone=BOT_TZ)

    # Проверка задач — каждые CHECK_INTERVAL секунд
    # FIX-4: max_instances + coalesce + misfire_grace_time
    scheduler.add_job(
        check_all_users, "interval",
        seconds=CHECK_INTERVAL,
        args=[client, redmine],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    # Утренний отчёт — 09:00
    scheduler.add_job(daily_report, "cron", hour=9, minute=0,
                      args=[client, redmine])

    # Очистка state в Postgres — 03:00
    scheduler.add_job(cleanup_state_files, "cron", hour=3, minute=0,
                      args=[redmine])

    scheduler.start()
    logger.info(
        f"✅ Планировщик: каждые {CHECK_INTERVAL}с, таймзона {BOT_TZ}, "
        f"пользователей: {len(USERS)}"
    )

    # --- Heartbeat (мониторинг живучести) ---
    import httpx
    BOT_INSTANCE_ID = str(uuid.uuid4())
    # Админка в Docker-сети доступна по имени сервиса 'admin'
    ADMIN_URL = os.getenv("ADMIN_URL", "http://admin:8080")
    HEARTBEAT_URL = f"{ADMIN_URL.rstrip('/')}/api/bot/heartbeat" if ADMIN_URL else None

    async def send_heartbeat():
        """Отправляет heartbeat на админку раз в 60 секунд."""
        if not HEARTBEAT_URL:
            return
        async with httpx.AsyncClient(timeout=10) as http:
            while True:
                try:
                    await http.post(
                        HEARTBEAT_URL,
                        json={"instance_id": BOT_INSTANCE_ID},
                    )
                except Exception as e:
                    logger.debug("Heartbeat failed: %s", e)
                await asyncio.sleep(60)

    if HEARTBEAT_URL:
        logger.info(f"📡 Heartbeat: отправка на {HEARTBEAT_URL}")
        asyncio.create_task(send_heartbeat())

    # --- Основной цикл ---
    try:
        logger.info("💤 Бот работает, проверки по расписанию...")
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("👋 Бот остановлен")
    finally:
        scheduler.shutdown(wait=False)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())