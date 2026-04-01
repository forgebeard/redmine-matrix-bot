"""Lifespan FastAPI: fail-fast проверки при старте."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from security import SecurityError, load_master_key


@asynccontextmanager
async def admin_lifespan(_app: FastAPI):
    try:
        load_master_key()
    except SecurityError as e:
        raise RuntimeError(f"startup failed: {e}") from e
    yield
