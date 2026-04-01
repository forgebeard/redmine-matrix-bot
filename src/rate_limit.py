"""Простой in-memory rate limiter (на процесс) для публичных auth-эндпоинтов админки."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class SimpleRateLimiter:
    """Скользящее окно: не более `limit` событий за `window_seconds` по ключу."""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        q = self._buckets[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True
