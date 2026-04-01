"""Юнит-тесты для admin.authz."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from admin.authz import require_admin


def test_require_admin_rejects_missing_user():
    req = MagicMock()
    req.state.current_user = None
    with pytest.raises(HTTPException) as ei:
        require_admin(req)
    assert ei.value.status_code == 403


def test_require_admin_rejects_non_admin_role():
    req = MagicMock()
    u = MagicMock()
    u.role = "user"
    req.state.current_user = u
    with pytest.raises(HTTPException) as ei:
        require_admin(req)
    assert ei.value.status_code == 403


def test_require_admin_accepts_and_returns_user():
    req = MagicMock()
    u = MagicMock()
    u.role = "admin"
    req.state.current_user = u
    assert require_admin(req) is u
