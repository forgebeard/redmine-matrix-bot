"""Async engine и сессии SQLAlchemy."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.url_resolver import materialize_database_url_env


def async_database_url(url: str | None) -> str:
    """postgresql:// → postgresql+asyncpg://"""
    if not url:
        return ""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql+asyncpg://"):
        return url
    raise ValueError("Ожидается DATABASE_URL с префиксом postgresql://")


def sync_database_url_for_alembic(url: str) -> str:
    """Для Alembic (sync): postgresql+psycopg://"""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    return url


def make_engine():
    materialize_database_url_env()
    url = os.getenv("DATABASE_URL", "")
    async_url = async_database_url(url)
    if not async_url:
        raise RuntimeError("DATABASE_URL не задан")
    return create_async_engine(async_url, echo=os.getenv("SQL_ECHO", "0") == "1")


_engine = None
_session_factory = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        _engine = make_engine()
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_session_factory():
    get_engine()
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine():
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_session():
    """FastAPI Depends: одна транзакция на запрос."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
