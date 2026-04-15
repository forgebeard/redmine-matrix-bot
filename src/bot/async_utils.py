"""Утилиты для асинхронного запуска синхронного кода."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="redmine")


async def run_in_thread(func, *args, **kwargs):
    """Запуск синхронной функции в отдельном потоке (не блокирует event loop)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, partial(func, *args, **kwargs))