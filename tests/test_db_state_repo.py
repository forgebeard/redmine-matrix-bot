import asyncio
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, text

from database.models import BotUserLease


async def _delete_lease_for_uid(factory, uid: int) -> None:
    """Убирает строку lease, чтобы тесты были идемпотентны на одной БД (локально / повторный pytest)."""
    async with factory() as session:
        await session.execute(delete(BotUserLease).where(BotUserLease.user_redmine_id == uid))
        await session.commit()


DATABASE_URL = os.getenv("DATABASE_URL", "")

# Чтобы импорт `bot` был предсказуемым (timezone/флаги).
os.environ.setdefault("BOT_TIMEZONE", "UTC")


def _needs_skip() -> bool:
    if not DATABASE_URL:
        return True
    if not DATABASE_URL.startswith("postgresql://"):
        # alembic/env.py и наш async конвертер ожидают postgresql://
        return True
    return False


async def ensure_migrated():
    if _needs_skip():
        pytest.skip("DATABASE_URL не задан (или формат не postgresql://)")

    # Проверяем, существует ли таблица — если нет, делаем alembic upgrade head.
    from database.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        res = await session.execute(text("SELECT to_regclass('bot_issue_state')"))
        reg = res.scalar()
        if reg is None:
            proc = await asyncio.to_thread(
                lambda: subprocess.run(["alembic", "upgrade", "head"], check=False)
            )
            if proc.returncode != 0:
                raise RuntimeError("alembic upgrade head failed")


@pytest.mark.asyncio
async def test_lease_single_owner():
    await ensure_migrated()
    from database.session import get_session_factory
    from database.state_repo import try_acquire_user_lease

    factory = get_session_factory()
    owner1 = uuid.uuid4()
    owner2 = uuid.uuid4()
    uid = 910_001
    await _delete_lease_for_uid(factory, uid)

    async with factory() as session:
        until1 = datetime.now(timezone.utc) + timedelta(seconds=300)
        ok1 = await try_acquire_user_lease(session, uid, owner1, until1)
        await session.commit()
        assert ok1 is True

        until2 = datetime.now(timezone.utc) + timedelta(seconds=300)
        ok2 = await try_acquire_user_lease(session, uid, owner2, until2)
        await session.commit()
        assert ok2 is False


@pytest.mark.asyncio
async def test_lease_expiry_allows_new_owner():
    await ensure_migrated()
    from database.session import get_session_factory
    from database.state_repo import try_acquire_user_lease

    factory = get_session_factory()
    owner1 = uuid.uuid4()
    owner2 = uuid.uuid4()
    uid = 910_002
    await _delete_lease_for_uid(factory, uid)

    async with factory() as session:
        until1 = datetime.now(timezone.utc) + timedelta(seconds=300)
        ok1 = await try_acquire_user_lease(session, uid, owner1, until1)
        await session.commit()
        assert ok1 is True

        # истекаем lease в прошлом
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.execute(
            text(
                "UPDATE bot_user_leases SET lease_until = :past, lease_owner_id = :owner WHERE user_redmine_id = :uid"
            ).bindparams(past=past, owner=owner1, uid=uid)
        )
        await session.commit()

        until2 = datetime.now(timezone.utc) + timedelta(seconds=300)
        ok2 = await try_acquire_user_lease(session, uid, owner2, until2)
        await session.commit()
        assert ok2 is True


@pytest.mark.asyncio
async def test_upsert_and_load_state():
    await ensure_migrated()
    from database.session import get_session_factory
    from database.state_repo import load_user_issue_state, upsert_user_issue_state

    factory = get_session_factory()
    uid = 1972
    iid = "100"

    now = datetime.now(timezone.utc)
    sent_at = now - timedelta(minutes=10)
    reminder_at = now - timedelta(minutes=5)
    overdue_at = now - timedelta(days=1)

    sent = {iid: {"notified_at": sent_at.isoformat(), "status": "Новая"}}
    reminders = {iid: {"last_reminder": reminder_at.isoformat()}}
    overdue = {iid: {"last_notified": overdue_at.isoformat()}}
    journals = {iid: {"last_journal_id": 42}}

    async with factory() as session:
        await upsert_user_issue_state(
            session,
            user_redmine_id=uid,
            issue_ids={iid},
            sent=sent,
            reminders=reminders,
            overdue=overdue,
            journals=journals,
        )
        await session.commit()

        loaded_sent, loaded_rem, loaded_over, loaded_jrn = await load_user_issue_state(session, uid)

    assert loaded_sent[iid]["status"] == "Новая"
    assert "last_reminder" in loaded_rem[iid]
    assert "last_notified" in loaded_over[iid]
    assert loaded_jrn[iid]["last_journal_id"] == 42


@pytest.mark.asyncio
async def test_reminder_and_overdue_conditions_from_loaded_state():
    await ensure_migrated()
    from database.session import get_session_factory
    from database.state_repo import load_user_issue_state, upsert_user_issue_state

    from src.bot.main import REMINDER_AFTER, ensure_tz

    factory = get_session_factory()
    uid = 1972
    iid = "100"

    now = datetime.now(timezone.utc)
    sent_at = now - timedelta(minutes=10)
    reminder_at = now - timedelta(seconds=REMINDER_AFTER + 10)
    overdue_at = now - timedelta(days=2)

    sent = {iid: {"notified_at": sent_at.isoformat(), "status": "Информация предоставлена"}}
    reminders = {iid: {"last_reminder": reminder_at.isoformat()}}
    overdue = {iid: {"last_notified": overdue_at.isoformat()}}
    journals = {}

    async with factory() as session:
        await upsert_user_issue_state(
            session,
            user_redmine_id=uid,
            issue_ids={iid},
            sent=sent,
            reminders=reminders,
            overdue=overdue,
            journals=journals,
        )
        await session.commit()

        loaded_sent, loaded_rem, loaded_over, _ = await load_user_issue_state(session, uid)

    last_rem = ensure_tz(datetime.fromisoformat(loaded_rem[iid]["last_reminder"]))
    time_since = (now - last_rem).total_seconds()
    assert time_since >= REMINDER_AFTER

    last_notified = ensure_tz(datetime.fromisoformat(loaded_over[iid]["last_notified"]))
    assert last_notified.date() < now.date()


@pytest.mark.asyncio
async def test_concurrent_lease_one_winner():
    await ensure_migrated()
    from database.session import get_session_factory
    from database.state_repo import try_acquire_user_lease

    factory = get_session_factory()
    uid = 910_003
    owner1 = uuid.uuid4()
    owner2 = uuid.uuid4()
    await _delete_lease_for_uid(factory, uid)

    async def worker(owner):
        async with factory() as session:
            until = datetime.now(timezone.utc) + timedelta(seconds=300)
            ok = await try_acquire_user_lease(session, uid, owner, until)
            await session.commit()
            return ok

    a, b = await asyncio.gather(worker(owner1), worker(owner2))
    assert (a is True and b is False) or (a is False and b is True)

