"""Состояние процесса админки: логгер, rate limiter, кэши, circuit breaker."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from admin.constants import ADMIN_EXISTS_CACHE_TTL_SECONDS, INTEGRATION_STATUS_CACHE_TTL_SECONDS
from rate_limit import SimpleRateLimiter

logger = logging.getLogger("admin")

rate_limiter = SimpleRateLimiter()


class AdminExistsCache:
    def __init__(self) -> None:
        self.value: bool | None = None
        self.expires_ts: float = 0.0

    def get(self) -> bool | None:
        if self.value is None:
            return None
        if datetime.now().timestamp() >= self.expires_ts:
            return None
        return self.value

    def set(self, value: bool) -> None:
        self.value = value
        self.expires_ts = datetime.now().timestamp() + ADMIN_EXISTS_CACHE_TTL_SECONDS

    def invalidate(self) -> None:
        self.value = None
        self.expires_ts = 0.0


class IntegrationStatusCache:
    def __init__(self) -> None:
        self.value: dict | None = None
        self.expires_ts: float = 0.0

    def get(self) -> dict | None:
        if self.value is None:
            return None
        if datetime.now().timestamp() >= self.expires_ts:
            return None
        return self.value

    def set(self, value: dict) -> None:
        self.value = value
        self.expires_ts = datetime.now().timestamp() + INTEGRATION_STATUS_CACHE_TTL_SECONDS

    def invalidate(self) -> None:
        self.value = None
        self.expires_ts = 0.0


class RedmineSearchBreaker:
    """In-memory circuit breaker для поиска пользователей Redmine."""

    def __init__(self) -> None:
        self.failures = 0
        self.cooldown_until_ts = 0.0

    def blocked(self) -> bool:
        return datetime.now().timestamp() < self.cooldown_until_ts

    def on_success(self) -> None:
        self.failures = 0
        self.cooldown_until_ts = 0.0

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures >= 5:
            self.cooldown_until_ts = datetime.now().timestamp() + 60


admin_exists_cache = AdminExistsCache()
integration_status_cache = IntegrationStatusCache()
redmine_search_breaker = RedmineSearchBreaker()

process_started_at = time.monotonic()
