#!/usr/bin/env python3
"""
Redmine → Matrix бот уведомлений.

Функции:
  1. Уведомление о новых задачах (статус «Новая»)
  2. Уведомление «Информация предоставлена» + напоминания каждый час
  3. Уведомление о просроченных задачах (раз в сутки)
  4. Уведомление о смене статуса задачи
  5. Уведомление о новых комментариях и изменениях (journals)
  6. Маршрутизация по статусу → разные комнаты Matrix (STATUS_ROOM_MAP)
  7. Дублирование новых задач проекта в общую комнату команды
  8. Ежедневный утренний отчёт (09:00)
  9. Автоочистка state-файлов от закрытых задач (03:00)

Требования:
  pip install matrix-nio python-redmine python-dotenv apscheduler

Настройка: все параметры через .env файл
Запуск: python3 bot.py или systemd-сервис
"""

# ── Стандартная библиотека ──────────────────────────────────────────────────
import asyncio
import json
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Сторонние библиотеки ───────────────────────────────────────────────────
from dotenv import load_dotenv
from nio import AsyncClient
from redminelib import Redmine
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()


# ═══════════════════════════════════════════════════════════════════════════
# НАСТРОЙКИ — все значения из .env
# ═══════════════════════════════════════════════════════════════════════════

# Matrix-сервер и авторизация
HOMESERVER       = os.getenv("MATRIX_HOMESERVER")
ACCESS_TOKEN     = os.getenv("MATRIX_ACCESS_TOKEN")
MATRIX_USER_ID   = os.getenv("MATRIX_USER_ID")
MATRIX_DEVICE_ID = os.getenv("MATRIX_DEVICE_ID")

# Matrix-комнаты
PERSONAL_ROOM_ID = os.getenv("MATRIX_ROOM_ID")         # Личная — все уведомления
TEAM_ROOM_ID     = os.getenv("MATRIX_TEAM_ROOM_ID")    # Командная — по проекту (опционально)

# Маршрутизация: статус задачи → дополнительная комната
# Формат в .env: STATUS_ROOM_MAP={"Передано в работу.РВ": "!room:server"}
# Задачи с указанными статусами ДОПОЛНИТЕЛЬНО отправляются в эту комнату
# Личная комната всегда получает уведомления
_status_room_raw = os.getenv("STATUS_ROOM_MAP", "{}")
try:
    STATUS_ROOM_MAP = json.loads(_status_room_raw)
except (json.JSONDecodeError, TypeError):
    STATUS_ROOM_MAP = {}

# Redmine
REDMINE_URL = os.getenv("REDMINE_URL")
REDMINE_KEY = os.getenv("REDMINE_API_KEY")

# State-файлы — хранят состояние бота между перезапусками
BASE_DIR       = Path(__file__).resolve().parent
SENT_FILE      = BASE_DIR / "sent_issues.json"      # Задачи, о которых уже уведомили + их статусы
REMINDERS_FILE = BASE_DIR / "reminders.json"         # Время последних напоминаний
OVERDUE_FILE   = BASE_DIR / "overdue_issues.json"    # Дата последнего уведомления о просрочке
JOURNALS_FILE  = BASE_DIR / "journals.json"          # Последний journal_id для каждой задачи
LOG_FILE       = BASE_DIR / "bot.log"

# Интервалы
CHECK_INTERVAL = 300    # Проверка Redmine каждые 5 минут
REMINDER_AFTER = 3600   # Напоминание «Информация предоставлена» каждый час

# Фильтр для общей комнаты команды — только задачи этого проекта
PROJECT_NAME_FOR_TEAM = "Ред Вирт"

# Таймзона
BOT_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Irkutsk"))

# Названия статусов Redmine (должны совпадать с вашим Redmine)
STATUS_NEW           = "Новая"
STATUS_INFO_PROVIDED = "Информация предоставлена"


# ═══════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ — файл (с ротацией 5 МБ × 5) + консоль
# ═══════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("redmine_bot")
logger.setLevel(logging.INFO)

_fh = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)

_ch = logging.StreamHandler()
_ch.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(_ch)


# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ — JSON, даты, маршрутизация
# ═══════════════════════════════════════════════════════════════════════════

def load_json(filepath, default=None):
    """Безопасная загрузка JSON. При ошибке возвращает default ({})."""
    filepath = Path(filepath)
    if filepath.exists():
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"❌ Ошибка чтения {filepath.name}: {e}")
    return default if default is not None else {}


def save_json(filepath, data):
    """Атомарная запись JSON: .tmp → rename. Защита от потери данных при крэше."""
    filepath = Path(filepath)
    tmp = filepath.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(filepath)
    except IOError as e:
        logger.error(f"❌ Ошибка записи {filepath.name}: {e}")


def plural_days(n):
    """Склонение: 1 день, 2 дня, 5 дней."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def now_tz():
    """Текущее время в таймзоне бота."""
    return datetime.now(tz=BOT_TZ)


def today_tz():
    """Текущая дата в таймзоне бота."""
    return now_tz().date()


def get_all_rooms_for_issue(issue, notification_type):
    """
    Определяет ВСЕ комнаты для отправки уведомления.

    Логика:
      1. Личная комната — ВСЕГДА
      2. Комната по статусу — ДОПОЛНИТЕЛЬНО (если статус есть в STATUS_ROOM_MAP)
      3. Командная комната — ДОПОЛНИТЕЛЬНО (новые задачи нужного проекта)

    Возвращает: set room_id (без дубликатов)
    """
    rooms = {PERSONAL_ROOM_ID}

    # Дополнительная комната по статусу задачи
    status_room = STATUS_ROOM_MAP.get(issue.status.name)
    if status_room:
        rooms.add(status_room)

    # Командная комната — только новые задачи определённого проекта
    if (TEAM_ROOM_ID
            and notification_type == "new"
            and issue.project.name == PROJECT_NAME_FOR_TEAM):
        rooms.add(TEAM_ROOM_ID)

    return rooms


# ═══════════════════════════════════════════════════════════════════════════
# MATRIX — отправка уведомлений
# ═══════════════════════════════════════════════════════════════════════════

# Типы уведомлений: ключ → (эмодзи, заголовок)
NOTIFICATION_TYPES = {
    "new":           ("🆕", "Новая задача"),
    "info":          ("✅", "Информация предоставлена"),
    "reminder":      ("⏰", "Напоминание"),
    "overdue":       ("⚠️", "Просроченная задача"),
    "status_change": ("🔄", "Смена статуса"),
    "issue_updated": ("📝", "Задача обновлена"),
}


async def send_matrix_message(client, issue, room_id, notification_type="info", extra_text=""):
    """Отправляет HTML-уведомление о задаче в одну Matrix-комнату."""
    issue_url = f"{REDMINE_URL}/issues/{issue.id}"
    emoji, title = NOTIFICATION_TYPES.get(notification_type, ("🔔", "Обратите внимание"))

    # Текст просрочки
    overdue_text = ""
    if notification_type == "overdue" and issue.due_date:
        days = (today_tz() - issue.due_date).days
        overdue_text = f" (просрочено на {plural_days(days)})"

    # HTML-сообщение
    html_body = (
        f"<p><strong>{emoji} {title}</strong></p>"
        f'<p><a href="{issue_url}">#{issue.id}</a> — {issue.subject}</p>'
        f"<p><em>{issue.project.name}</em></p>"
        f"<p>Статус: <strong>{issue.status.name}</strong></p>"
        f"<p>Приоритет: {issue.priority.name}</p>"
    )
    if extra_text:
        html_body += f"<p>{extra_text}</p>"
    if issue.due_date:
        html_body += f"<p>Срок: {issue.due_date}{overdue_text}</p>"
    html_body += f'<p>🔗 <a href="{issue_url}">Открыть задачу</a></p>'

    # Текстовый fallback (для клиентов без HTML)
    plain_body = (
        f"{emoji} {title} #{issue.id}: {issue.subject} "
        f"| {issue.project.name} | Статус: {issue.status.name}"
    )

    content = {
        "msgtype": "m.text",
        "body": plain_body,
        "format": "org.matrix.custom.html",
        "formatted_body": html_body,
    }

    await client.room_send(room_id=room_id, message_type="m.room.message", content=content)

    # Определяем человекочитаемое название комнаты для лога
    if room_id == PERSONAL_ROOM_ID:
        room_label = "личную"
    elif room_id == TEAM_ROOM_ID:
        room_label = "командную"
    elif room_id in STATUS_ROOM_MAP.values():
        room_label = f"статусную ({issue.status.name})"
    else:
        room_label = room_id
    logger.info(f"Отправлено в {room_label} комнату: #{issue.id} ({notification_type})")


async def send_to_all_rooms(client, issue, rooms, notification_type, extra_text=""):
    """Рассылает уведомление во все комнаты. Ошибка в одной не блокирует остальные."""
    for room_id in rooms:
        try:
            await send_matrix_message(client, issue, room_id, notification_type, extra_text)
        except Exception as e:
            logger.error(f"❌ Ошибка отправки в {room_id} (#{issue.id}, {notification_type}): {e}")


# ═══════════════════════════════════════════════════════════════════════════
# ДЕТЕКТОРЫ ИЗМЕНЕНИЙ — анализ задач Redmine
# ═══════════════════════════════════════════════════════════════════════════

def detect_status_change(issue, sent):
    """Возвращает старый статус, если он изменился с момента последнего уведомления. Иначе None."""
    issue_id = str(issue.id)
    if issue_id not in sent:
        return None
    old_status = sent[issue_id].get("status")
    if old_status and old_status != issue.status.name:
        return old_status
    return None


def detect_new_journals(issue, journals_state):
    """
    Находит новые записи в истории задачи (journals).
    Возвращает: (список_новых_journals, максимальный_journal_id)
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


# Человекочитаемые названия полей Redmine
FIELD_NAMES = {
    "status_id":        "Статус",
    "assigned_to_id":   "Назначена",
    "priority_id":      "Приоритет",
    "done_ratio":       "Готовность",
    "due_date":         "Срок",
    "subject":          "Тема",
    "description":      "Описание",
    "tracker_id":       "Трекер",
    "fixed_version_id": "Версия",
}


def describe_journal(journal):
    """
    Формирует краткое описание journal-записи.
    Возвращает строку вида: «💬 комментарий от Иванов; 📝 Статус: 1 → 3»
    Текст комментария НЕ включается (может быть огромным).
    """
    parts = []

    # Комментарий
    if journal.notes:
        try:
            parts.append(f"💬 комментарий от {journal.user.name}")
        except Exception:
            parts.append("💬 новый комментарий")

    # Изменения полей
    try:
        for detail in journal.details:
            prop = detail.get("name", detail.get("property", "?"))
            old_val = detail.get("old_value", "—")
            new_val = detail.get("new_value", "—")
            field_label = FIELD_NAMES.get(prop, prop)
            parts.append(f"📝 {field_label}: {old_val} → {new_val}")
    except Exception:
        if not parts:
            parts.append("📝 изменение в задаче")

    return "; ".join(parts) if parts else None


# ═══════════════════════════════════════════════════════════════════════════
# ЗАДАЧИ ПЛАНИРОВЩИКА — периодические проверки
# ═══════════════════════════════════════════════════════════════════════════

async def check_issues(client, redmine):
    """
    Основная проверка: получает задачи из Redmine, находит изменения,
    рассылает уведомления в нужные комнаты Matrix.
    Вызывается каждые CHECK_INTERVAL секунд.
    """
    logger.info(f"🔍 Проверка Redmine в {now_tz().strftime('%H:%M:%S')}...")

    try:
        issues = list(redmine.issue.filter(
            assigned_to_id="me", status_id="open", include=["journals"]
        ))
    except Exception as e:
        logger.error(f"❌ Ошибка Redmine API: {e}")
        return

    logger.info(f"📋 Получено задач: {len(issues)}")

    # Загружаем state
    sent             = load_json(SENT_FILE)
    reminders        = load_json(REMINDERS_FILE)
    overdue_notified = load_json(OVERDUE_FILE)
    journals_state   = load_json(JOURNALS_FILE)

    # Флаги — сохраняем файлы только при реальных изменениях
    sent_changed = reminders_changed = overdue_changed = journals_changed = False

    now   = now_tz()
    today = now.date()

    for issue in issues:
        issue_id = str(issue.id)

        # ── 1. Смена статуса ──────────────────────────────────────────
        # Если задача уже в sent и статус изменился — уведомляем
        old_status = detect_status_change(issue, sent)
        if old_status:
            rooms = get_all_rooms_for_issue(issue, "status_change")
            await send_to_all_rooms(
                client, issue, rooms, "status_change",
                extra_text=(
                    f"Статус изменён: <strong>{old_status}</strong>"
                    f" → <strong>{issue.status.name}</strong>"
                )
            )
            sent[issue_id]["status"] = issue.status.name
            sent_changed = True

        # ── 2. Новая задача (статус «Новая») ─────────────────────────
        if issue.status.name == STATUS_NEW and issue_id not in sent:
            rooms = get_all_rooms_for_issue(issue, "new")
            await send_to_all_rooms(client, issue, rooms, "new")
            sent[issue_id] = {"notified_at": now.isoformat(), "status": STATUS_NEW}
            sent_changed = True

        # ── 3. Информация предоставлена ───────────────────────────────
        if issue.status.name == STATUS_INFO_PROVIDED:
            if issue_id not in sent:
                # Первое уведомление
                rooms = get_all_rooms_for_issue(issue, "info")
                await send_to_all_rooms(client, issue, rooms, "info")
                sent[issue_id] = {"notified_at": now.isoformat(), "status": STATUS_INFO_PROVIDED}
                sent_changed = True
            else:
                # Напоминание каждый час — только в личную комнату
                notified_at = datetime.fromisoformat(sent[issue_id]["notified_at"])
                last_reminder = reminders.get(issue_id, {}).get("last_reminder")

                if last_reminder:
                    time_since = (now - datetime.fromisoformat(last_reminder)).total_seconds()
                else:
                    time_since = (now - notified_at).total_seconds()

                if time_since >= REMINDER_AFTER:
                    try:
                        await send_matrix_message(client, issue, PERSONAL_ROOM_ID, "reminder")
                        reminders[issue_id] = {"last_reminder": now.isoformat()}
                        reminders_changed = True
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки (reminder #{issue.id}): {e}")

        # ── 3.1. Первое уведомление для остальных задач ───────────────
        # Все задачи (кроме «Новая» и «Инфо») — регистрируем в sent
        # Если статус есть в STATUS_ROOM_MAP — дополнительно шлём в статусную комнату
        if (issue.status.name not in (STATUS_NEW, STATUS_INFO_PROVIDED)
                and issue_id not in sent):
            rooms = get_all_rooms_for_issue(issue, "new")
            await send_to_all_rooms(client, issue, rooms, "new")
            sent[issue_id] = {"notified_at": now.isoformat(), "status": issue.status.name}
            sent_changed = True
            logger.info(f"📌 Задача #{issue.id} добавлена в sent (статус: {issue.status.name})")

        # ── 4. Просроченные задачи (не чаще 1 раза в сутки) ──────────
        if issue.due_date and issue.due_date < today:
            last_notified = overdue_notified.get(issue_id, {}).get("last_notified")
            if not last_notified or (now - datetime.fromisoformat(last_notified)).days >= 1:
                rooms = get_all_rooms_for_issue(issue, "overdue")
                await send_to_all_rooms(client, issue, rooms, "overdue")
                overdue_notified[issue_id] = {"last_notified": now.isoformat()}
                overdue_changed = True

        # ── 5. Новые комментарии и изменения (journals) ──────────────
        new_journals, max_id = detect_new_journals(issue, journals_state)
        if new_journals and issue_id in sent:
            descriptions = [describe_journal(j) for j in new_journals]
            descriptions = [d for d in descriptions if d]  # убираем None

            if descriptions:
                combined = "<br/>".join(descriptions[-5:])
                if len(descriptions) > 5:
                    combined = f"<em>...и ещё {len(descriptions) - 5} изменений</em><br/>" + combined

                rooms = get_all_rooms_for_issue(issue, "issue_updated")
                await send_to_all_rooms(client, issue, rooms, "issue_updated", extra_text=combined)

        # Обновляем last_journal_id (всегда — даже если не отправляли уведомление)
        if max_id > journals_state.get(issue_id, {}).get("last_journal_id", 0):
            journals_state[issue_id] = {"last_journal_id": max_id}
            journals_changed = True

    # ── Сохраняем state-файлы при наличии изменений ───────────────────
    if sent_changed:
        save_json(SENT_FILE, sent)
    if reminders_changed:
        save_json(REMINDERS_FILE, reminders)
    if overdue_changed:
        save_json(OVERDUE_FILE, overdue_notified)
    if journals_changed:
        save_json(JOURNALS_FILE, journals_state)


async def daily_report(client, redmine):
    """Ежедневный утренний отчёт (09:00) — только в личную комнату."""
    logger.info("📊 Формируем утренний отчёт...")

    try:
        issues = list(redmine.issue.filter(assigned_to_id="me", status_id="open"))
    except Exception as e:
        logger.error(f"❌ Ошибка Redmine API при формировании отчёта: {e}")
        return

    today = today_tz()
    info_provided = [i for i in issues if i.status.name == STATUS_INFO_PROVIDED]
    overdue = sorted(
        [i for i in issues if i.due_date and i.due_date < today],
        key=lambda i: i.due_date
    )

    # Формируем HTML
    html = f"<h3>📅 Отчёт на {today.strftime('%d.%m.%Y')}</h3>"
    html += f"<p><strong>Всего открытых задач:</strong> {len(issues)}</p>"
    html += f"<p><strong>Задач «{STATUS_INFO_PROVIDED}»:</strong> {len(info_provided)}</p>"

    if info_provided:
        html += "<ul>"
        for i in info_provided[:10]:
            html += f'<li><a href="{REDMINE_URL}/issues/{i.id}">#{i.id}</a> — {i.subject}</li>'
        html += "</ul>"
        if len(info_provided) > 10:
            html += f"<p><em>...и ещё {len(info_provided) - 10}</em></p>"

    html += f"<p><strong>Просроченных задач:</strong> {len(overdue)}</p>"

    if overdue:
        html += "<ul>"
        for i in overdue[:10]:
            days = (today - i.due_date).days
            html += (
                f'<li><a href="{REDMINE_URL}/issues/{i.id}">#{i.id}</a>'
                f" — {i.subject} (просрочено на {plural_days(days)})</li>"
            )
        html += "</ul>"
        if len(overdue) > 10:
            html += f"<p><em>...и ещё {len(overdue) - 10}</em></p>"

    plain = f"Отчёт Redmine {today.strftime('%d.%m.%Y')}: задач {len(issues)}, просрочено {len(overdue)}"

    content = {
        "msgtype": "m.text",
        "body": plain,
        "format": "org.matrix.custom.html",
        "formatted_body": html,
    }

    try:
        await client.room_send(room_id=PERSONAL_ROOM_ID, message_type="m.room.message", content=content)
        logger.info(f"✅ Утренний отчёт: задач {len(issues)}, просрочено {len(overdue)}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки отчёта: {e}")


async def cleanup_state_files(redmine):
    """Очистка state-файлов от закрытых задач (03:00). Предотвращает рост JSON."""
    logger.info("🧹 Очистка state-файлов...")

    try:
        open_issues = list(redmine.issue.filter(assigned_to_id="me", status_id="open"))
    except Exception as e:
        logger.error(f"❌ Ошибка Redmine API при очистке: {e}")
        return

    open_ids = {str(i.id) for i in open_issues}
    cleaned_total = 0

    for filepath in [SENT_FILE, REMINDERS_FILE, OVERDUE_FILE, JOURNALS_FILE]:
        data = load_json(filepath)
        before = len(data)
        cleaned = {k: v for k, v in data.items() if k in open_ids}
        if len(cleaned) != before:
            save_json(filepath, cleaned)
            cleaned_total += before - len(cleaned)

    logger.info(f"🧹 Очистка завершена: удалено {cleaned_total} записей")


# ═══════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    logger.info("🚀 Redmine → Matrix бот запущен")

    # ── Проверка обязательных настроек ─────────────────────────────────
    required_env = {
        "MATRIX_HOMESERVER":   HOMESERVER,
        "MATRIX_ACCESS_TOKEN": ACCESS_TOKEN,
        "MATRIX_ROOM_ID":     PERSONAL_ROOM_ID,
        "MATRIX_USER_ID":     MATRIX_USER_ID,
        "REDMINE_URL":        REDMINE_URL,
        "REDMINE_API_KEY":    REDMINE_KEY,
    }
    missing = [name for name, value in required_env.items() if not value]
    if missing:
        logger.error(f"❌ Не заданы переменные окружения: {', '.join(missing)}")
        return

    # ── Лог маршрутизации ─────────────────────────────────────────────
    if STATUS_ROOM_MAP:
        logger.info(f"🗺️ Маршрутизация: {len(STATUS_ROOM_MAP)} правил")
        for status_name, room_id in STATUS_ROOM_MAP.items():
            logger.info(f"   📌 «{status_name}» → {room_id}")
    else:
        logger.info("🗺️ Маршрутизация: не настроена (все → личная комната)")

        # ── Matrix ────────────────────────────────────────────────────────
    client = AsyncClient(HOMESERVER)
    client.access_token = ACCESS_TOKEN
    client.user_id = MATRIX_USER_ID
    client.device_id = MATRIX_DEVICE_ID

    try:
        resp = await client.whoami()
        logger.info(f"✅ Matrix: подключён как {resp.user_id}")
    except Exception as e:
        logger.error(f"❌ Matrix недоступен: {e}")
        await client.close()
        return

    # ── Redmine ───────────────────────────────────────────────────────
    redmine = Redmine(REDMINE_URL, key=REDMINE_KEY)
    try:
        user = redmine.user.get("current")
        logger.info(f"✅ Redmine: подключён как {user.firstname} {user.lastname}")
    except Exception as e:
        logger.error(f"❌ Redmine недоступен: {e}")
        await client.close()
        return

    # ── Инициализация journals при первом запуске ─────────────────────
    # Запоминаем текущие journal_id, чтобы не спамить о старых изменениях
    journals_state = load_json(JOURNALS_FILE)
    if not journals_state:
        logger.info("📝 Первый запуск: инициализация journals...")
        try:
            init_issues = list(redmine.issue.filter(
                assigned_to_id="me", status_id="open", include=["journals"]
            ))
            for issue in init_issues:
                try:
                    all_journals = list(issue.journals)
                    if all_journals:
                        journals_state[str(issue.id)] = {
                            "last_journal_id": max(j.id for j in all_journals)
                        }
                except Exception:
                    pass
            save_json(JOURNALS_FILE, journals_state)
            logger.info(f"📝 Инициализировано journals для {len(journals_state)} задач")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации journals: {e}")

    # ── Планировщик ───────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone=BOT_TZ)
    scheduler.add_job(check_issues, "interval", seconds=CHECK_INTERVAL, args=[client, redmine])
    scheduler.add_job(daily_report, "cron", hour=9, minute=0, args=[client, redmine])
    scheduler.add_job(cleanup_state_files, "cron", hour=3, minute=0, args=[redmine])
    scheduler.start()
    logger.info(f"✅ Планировщик: проверка каждые {CHECK_INTERVAL}с, таймзона {BOT_TZ}")

    # ── Основной цикл ────────────────────────────────────────────────
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