"""Хеш одноразового кода привязки Matrix-комнаты."""

from __future__ import annotations

import hashlib

from admin.constants import AUTH_TOKEN_SALT


def hash_binding_code(plain: str) -> str:
    return hashlib.sha256((plain + AUTH_TOKEN_SALT).encode("utf-8")).hexdigest()
