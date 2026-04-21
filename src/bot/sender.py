"""Отправка сообщений в Matrix.

Формирование тела сообщения через именованные tpl-шаблоны, отправка с retry.
"""

from __future__ import annotations

import asyncio
import html as html_module
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.logic import NOTIFICATION_TYPES, plural_days
from bot.notification_template_routing import EVENT_TO_TEMPLATE
from bot.time_context import notify_context_for_room
from matrix_send import room_send_with_retry
from preferences import can_notify

if TYPE_CHECKING:
    from nio import AsyncClient
    from redminelib.resources import Issue

logger = logging.getLogger("redmine_bot")


def _strip_html_to_plain(html: str) -> str:
    """Грубое снятие тегов для Matrix body при отсутствии body_plain в БД."""
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return html_module.unescape(re.sub(r"\s+", " ", t)).strip()


def _elapsed_human_since(dt: datetime | None) -> str:
    if not isinstance(dt, datetime):
        return ""
    src = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    now_u = datetime.now(UTC)
    seconds = max(0, int((now_u - src.astimezone(UTC)).total_seconds()))
    if seconds < 60:
        return "меньше минуты"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def _plain_prefixed(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        if line:
            out.append(f"| {line}")
        else:
            out.append("|")
    return "\n".join(out)


def _v5_plain_issue_update(context: dict[str, Any]) -> str:
    issue_id = str(context.get("issue_id", "") or "")
    subject = str(context.get("subject", "") or "").strip()
    title = f"#{issue_id} — {subject}" if subject else f"#{issue_id}"
    lines = [
        "Задача обновлена",
        title,
        "",
        f"Проект: {context.get('project_name') or '—'}",
        f"Версия: {context.get('version_line') or context.get('version') or '—'}",
        f"Статус: {context.get('status_line') or context.get('status') or '—'}",
        f"Приоритет: {context.get('priority_line') or context.get('priority') or '—'}",
        f"Исполнитель: {context.get('assignee_line') or context.get('assignee_name') or '—'}",
        "",
        str(context.get("issue_url", "") or ""),
    ]
    return _plain_prefixed(lines)

# ── Config (заполняется из main.py при старте) ──────────────────────────────

REDMINE_URL: str = ""
PORTAL_BASE_URL: str = ""

# ── Таймаут на создание DM-комнаты (секунды) ────────────────────────────────

DM_CREATE_TIMEOUT: int = 60

# ── Пауза между созданиями DM (чтобы не упереться в rate-limit) ─────────────

DM_CREATE_DELAY: float = 5.0

# ── Кеш MXID → room_id (чтобы не искать/создавать DM каждый раз) ────────────

_mxid_to_room_cache: dict[str, str] = {}

# ── Множество MXID, для которых создание DM провалилось в текущем цикле ─────

_dm_failed: set[str] = set()


def reset_dm_failed() -> None:
    """Сброс списка неудачных DM (вызывается в начале каждого цикла)."""
    _dm_failed.clear()


async def prewarm_dm_rooms(client: AsyncClient, mxids: list[str]) -> None:
    """Предварительное создание/поиск DM-комнат для списка MXID.

    Вызывается один раз при старте бота, после первого sync.
    Последовательно (не параллельно), чтобы не упереться в rate-limit homeserver'а.
    """
    to_resolve: list[str] = []
    found_in_cache = 0

    for m in mxids:
        m = (m or "").strip()
        if not m or m.startswith("!"):
            continue
        if not m.startswith("@"):
            m = f"@{m}"
        if m in _mxid_to_room_cache:
            found_in_cache += 1
            continue
        if m not in to_resolve:
            to_resolve.append(m)

    if not to_resolve:
        logger.info("🔗 Pre-warm DM: все %d комнат уже в кеше", found_in_cache)
        return

    logger.info(
        "🔗 Pre-warm DM: %d в кеше, %d нужно резолвить...",
        found_in_cache,
        len(to_resolve),
    )

    created_count = 0
    found_count = 0
    failed_count = 0
    need_create: list[str] = []

    # Фаза 1: быстрый поиск среди уже загруженных комнат (без API-вызовов)
    bot_mxid = client.user_id
    for target_mxid in to_resolve:
        room_id = _find_existing_dm(client, target_mxid, bot_mxid)
        if room_id:
            _mxid_to_room_cache[target_mxid] = room_id
            found_count += 1
            logger.info("🔗 DM найден: %s → %s", target_mxid, room_id)
        else:
            need_create.append(target_mxid)

    # Фаза 2: создание недостающих DM — по одному, с паузой
    for i, target_mxid in enumerate(need_create):
        try:
            room_id = await asyncio.wait_for(
                _create_dm(client, target_mxid),
                timeout=DM_CREATE_TIMEOUT,
            )
            _mxid_to_room_cache[target_mxid] = room_id
            created_count += 1
            logger.info(
                "✅ DM создан (%d/%d): %s → %s",
                i + 1,
                len(need_create),
                target_mxid,
                room_id,
            )
        except TimeoutError:
            logger.warning("⏱ Pre-warm DM timeout: %s (пропуск)", target_mxid)
            failed_count += 1
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "ratelimited" in error_msg.lower():
                # Rate-limited — ждём дольше и пробуем дальше
                logger.warning(
                    "⚠ Rate-limited при создании DM %s, пауза 60с...",
                    target_mxid,
                )
                await asyncio.sleep(60)
                # Повторная попытка
                try:
                    room_id = await asyncio.wait_for(
                        _create_dm(client, target_mxid),
                        timeout=DM_CREATE_TIMEOUT,
                    )
                    _mxid_to_room_cache[target_mxid] = room_id
                    created_count += 1
                    logger.info("✅ DM создан (retry): %s → %s", target_mxid, room_id)
                except Exception as retry_err:
                    logger.warning("⚠ Pre-warm DM retry failed: %s — %s", target_mxid, retry_err)
                    failed_count += 1
            else:
                logger.warning("⚠ Pre-warm DM ошибка: %s — %s", target_mxid, e)
                failed_count += 1

        # Пауза между созданиями (не после последнего)
        if i < len(need_create) - 1:
            await asyncio.sleep(DM_CREATE_DELAY)

    logger.info(
        "✅ Pre-warm DM завершён: %d найдено, %d создано, %d неудачно (всего %d)",
        found_count,
        created_count,
        failed_count,
        len(to_resolve),
    )
    # Сбрасываем failed — при первом цикле попробуем ещё раз
    _dm_failed.clear()


def _find_existing_dm(client: AsyncClient, target_mxid: str, bot_mxid: str) -> str | None:
    """Ищет существующую DM-комнату среди загруженных комнат. Не делает API-вызовов."""
    for r_id, room_obj in client.rooms.items():
        members = set()
        if hasattr(room_obj, "users"):
            members = {m for m in room_obj.users}
        elif hasattr(room_obj, "members"):
            members = set(room_obj.members)

        if target_mxid in members and bot_mxid in members and len(members) == 2:
            return r_id
    return None


async def _create_dm(client: AsyncClient, target_mxid: str) -> str:
    """Создаёт DM-комнату. Бросает исключение при ошибке."""
    from nio import RoomCreateError, RoomCreateResponse

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        resp = await client.room_create(
            invite=[target_mxid],
            is_direct=True,
        )

        if isinstance(resp, RoomCreateResponse) and resp.room_id:
            return resp.room_id

        # nio может вернуть RoomCreateError с кодом M_LIMIT_EXCEEDED
        if isinstance(resp, RoomCreateError):
            msg = resp.message or ""
            status = getattr(resp, "status_code", None) or ""
            if "LIMIT_EXCEEDED" in msg.upper() or str(status) == "429":
                retry_ms = 30000  # по умолчанию 30с
                # Пытаемся достать retry_after_ms из ответа
                if hasattr(resp, "retry_after_ms") and resp.retry_after_ms:
                    retry_ms = resp.retry_after_ms
                wait_s = (retry_ms / 1000) + 2  # +2с запас
                if attempt < max_attempts:
                    logger.warning(
                        "⚠ Rate-limited при создании DM %s (попытка %d/%d), ждём %.0fс...",
                        target_mxid,
                        attempt,
                        max_attempts,
                        wait_s,
                    )
                    await asyncio.sleep(wait_s)
                    continue

        raise RuntimeError(f"Не удалось создать DM с {target_mxid}: {resp}")

    raise RuntimeError(f"Не удалось создать DM с {target_mxid} после {max_attempts} попыток")


async def _resolve_room_id(client: AsyncClient, room_or_mxid: str) -> str:
    """Если room_or_mxid — MXID (@user:server), находит или создаёт DM.

    Если это уже room_id (!xxx:server), возвращает как есть.
    Результат кешируется на время жизни процесса.
    """
    room_or_mxid = (room_or_mxid or "").strip()

    # Уже нормальный room_id
    if room_or_mxid.startswith("!"):
        return room_or_mxid

    # Это MXID — нужно найти или создать DM
    target_mxid = room_or_mxid
    if not target_mxid.startswith("@"):
        target_mxid = f"@{target_mxid}"

    # Проверяем кеш
    if target_mxid in _mxid_to_room_cache:
        return _mxid_to_room_cache[target_mxid]

    # Если уже провалилось в этом цикле — не повторяем
    if target_mxid in _dm_failed:
        raise RuntimeError(f"DM с {target_mxid} уже не удался в этом цикле (пропуск)")

    bot_mxid = client.user_id

    # Синхронизируем список комнат (нужен хотя бы один sync)
    if not client.rooms:
        logger.info("📡 Matrix sync (первый раз, для поиска DM)...")
        await client.sync(timeout=10000, full_state=True)

    # Ищем существующую DM-комнату
    room_id = _find_existing_dm(client, target_mxid, bot_mxid)
    if room_id:
        logger.info("🔗 DM найден: %s → %s", target_mxid, room_id)
        _mxid_to_room_cache[target_mxid] = room_id
        return room_id

    # Создаём новую DM с таймаутом
    logger.info("📨 Создаём DM с %s...", target_mxid)
    try:
        new_room_id = await asyncio.wait_for(
            _create_dm(client, target_mxid),
            timeout=DM_CREATE_TIMEOUT,
        )
        logger.info("✅ DM создан: %s → %s", target_mxid, new_room_id)
        _mxid_to_room_cache[target_mxid] = new_room_id
        return new_room_id
    except TimeoutError:
        _dm_failed.add(target_mxid)
        raise RuntimeError(f"Таймаут создания DM с {target_mxid} ({DM_CREATE_TIMEOUT}с)")
    except Exception:
        _dm_failed.add(target_mxid)
        raise


async def resolve_room(client: AsyncClient, room_or_mxid: str) -> str:
    """Публичный хелпер: резолвит MXID → room_id через кеш. Для использования вне sender."""
    return await _resolve_room_id(client, room_or_mxid)


async def _tpl_build_matrix_message_content(
    session: AsyncSession,
    issue: Issue,
    notification_type: str,
    extra_text: str,
) -> dict:
    from bot.config_state import CATALOGS
    from bot.template_context import build_issue_context
    from bot.template_loader import render_named_template

    tpl_name = EVENT_TO_TEMPLATE[notification_type]
    emoji, title = NOTIFICATION_TYPES.get(notification_type, ("", "Обратите внимание"))
    catalogs = CATALOGS

    extra_merged = (extra_text or "").strip()
    if notification_type == "overdue" and issue.due_date:
        from bot.main import today_tz

        days = (today_tz() - issue.due_date).days
        ov_line = f"просрочено на {plural_days(days)}"
        if ov_line not in extra_merged:
            extra_merged = ov_line + (f"<br/>{extra_merged}" if extra_merged else "")

    event_label = NOTIFICATION_TYPES[notification_type][1]

    if tpl_name == "tpl_reminder":
        ctx = build_issue_context(
            issue,
            catalogs,
            reminder_text="Задача без движения",
            title="Напоминание",
            emoji="",
            reminder_count=1,
            max_reminders=max(1, int(catalogs.cycle_int("MAX_REMINDERS", 3))) if catalogs else 1,
            elapsed_human=_elapsed_human_since(getattr(issue, "updated_on", None)),
        )
    else:
        ctx = build_issue_context(
            issue,
            catalogs,
            emoji=emoji,
            title=title,
            event_type=event_label,
            extra_text=extra_merged,
        )

    html_out, plain_opt = await render_named_template(session, tpl_name, ctx)
    if notification_type in {"issue_updated", "status_change"}:
        plain_body = _v5_plain_issue_update(ctx)
    else:
        plain_body = (plain_opt or "").strip() or _strip_html_to_plain(html_out)
    return {
        "msgtype": "m.text",
        "body": plain_body,
        "format": "org.matrix.custom.html",
        "formatted_body": html_out,
    }


async def build_matrix_message_content(
    issue: Issue,
    notification_type: str,
    extra_text: str = "",
    *,
    session: AsyncSession | None = None,
) -> dict:
    """Собирает тело ``m.room.message`` для DLQ и отправки (без резолва комнаты).

    Нужен ``session`` для чтения override из ``notification_templates``.
    """
    if session is None:
        raise RuntimeError(
            "build_matrix_message_content: AsyncSession required "
            f"(issue #{getattr(issue, 'id', '?')}, type={notification_type})"
        )
    return await _tpl_build_matrix_message_content(session, issue, notification_type, extra_text)


async def send_matrix_message(
    client: AsyncClient,
    issue: Issue,
    room_id: str,
    notification_type: str,
    extra_text: str = "",
    *,
    session: AsyncSession | None = None,
    txn_id: str | None = None,
) -> None:
    """Формирует и отправляет HTML-сообщение в Matrix через tpl-шаблоны."""
    resolved_room = await _resolve_room_id(client, room_id)
    content = await build_matrix_message_content(
        issue, notification_type, extra_text=extra_text, session=session
    )
    await room_send_with_retry(client, resolved_room, content, txn_id=txn_id)
    logger.info("📨 #%s → %s... (%s)", issue.id, resolved_room[:20], notification_type)


async def send_safe(
    client: AsyncClient,
    issue: Issue,
    user_cfg: dict,
    room_id: str,
    notification_type: str,
    extra_text: str = "",
    db_session: AsyncSession | None = None,
    txn_id: str | None = None,
) -> None:
    """Обёртка: проверка DND/рабочих часов → отправка с перехватом ошибок."""
    from bot.logic import _cfg_for_room, _issue_priority_name, issue_matches_cfg

    cfg = _cfg_for_room(user_cfg, room_id)
    nctx = notify_context_for_room(user_cfg, room_id)
    if not issue_matches_cfg(issue, cfg):
        logger.debug(
            "Пропуск (атрибуты): user %s, #%s, room=%s",
            user_cfg.get("redmine_id"),
            issue.id,
            room_id[:20],
        )
        return
    if not can_notify(cfg, priority=_issue_priority_name(issue), context=nctx):
        logger.debug(
            "Пропуск (время/DND): user %s, #%s, %s",
            user_cfg.get("redmine_id"),
            issue.id,
            notification_type,
        )
        return
    try:
        await send_matrix_message(
            client,
            issue,
            room_id,
            notification_type,
            extra_text,
            session=db_session,
            txn_id=txn_id,
        )
    except Exception as e:
        logger.error("❌ Ошибка отправки #%s → %s: %s", issue.id, room_id[:20], e)
        # Сохраняем в DLQ для повторной отправки
        if db_session is not None:
            try:
                from database.dlq_repo import enqueue_notification

                payload = await build_matrix_message_content(
                    issue, notification_type, extra_text=extra_text, session=db_session
                )
                await enqueue_notification(
                    db_session,
                    user_redmine_id=user_cfg.get("redmine_id", 0),
                    issue_id=issue.id,
                    room_id=room_id,
                    notification_type=notification_type,
                    payload=payload,
                    error=str(e),
                )
            except Exception as dlq_err:
                logger.error("❌ Не удалось сохранить в DLQ: %s", dlq_err)
