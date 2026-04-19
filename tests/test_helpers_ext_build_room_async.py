"""Async tests for admin.helpers_ext._build_room_id_async."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from admin.helpers_ext import _build_room_id_async


@pytest.fixture
def fake_session() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_build_room_bare_localpart_to_mxid(monkeypatch, fake_session):
    async def fake_load(_session, name: str) -> str:
        if name == "MATRIX_USER_ID":
            return "@bot:example.com"
        return ""

    monkeypatch.setattr("admin.helpers_ext._load_secret_plain", fake_load)
    assert await _build_room_id_async("ivan", fake_session) == "@ivan:example.com"


@pytest.mark.asyncio
async def test_build_room_full_mxid_unchanged(monkeypatch, fake_session):
    async def fake_load(_session, name: str) -> str:
        if name == "MATRIX_USER_ID":
            return "@bot:example.com"
        return ""

    monkeypatch.setattr("admin.helpers_ext._load_secret_plain", fake_load)
    assert await _build_room_id_async("@ivan:other.hs", fake_session) == "@ivan:other.hs"


@pytest.mark.asyncio
async def test_build_room_at_without_colon_normalized(monkeypatch, fake_session):
    async def fake_load(_session, name: str) -> str:
        if name == "MATRIX_USER_ID":
            return "@bot:example.com"
        return ""

    monkeypatch.setattr("admin.helpers_ext._load_secret_plain", fake_load)
    assert await _build_room_id_async("@ivan", fake_session) == "@ivan:example.com"


@pytest.mark.asyncio
async def test_build_room_explicit_room_id_unchanged(monkeypatch, fake_session):
    async def fake_load(_session, name: str) -> str:
        if name == "MATRIX_USER_ID":
            return "@bot:example.com"
        return ""

    monkeypatch.setattr("admin.helpers_ext._load_secret_plain", fake_load)
    rid = "!abcDEFgh12:matrix.org"
    assert await _build_room_id_async(rid, fake_session) == rid


@pytest.mark.asyncio
async def test_build_room_empty_or_no_domain_returns_localpart(monkeypatch, fake_session):
    async def fake_load_empty(_session, name: str) -> str:
        if name == "MATRIX_USER_ID":
            return ""
        return ""

    monkeypatch.setattr("admin.helpers_ext._load_secret_plain", fake_load_empty)
    assert await _build_room_id_async("ivan", fake_session) == "ivan"

    async def fake_load_no_colon(_session, name: str) -> str:
        if name == "MATRIX_USER_ID":
            return "invalid"
        return ""

    monkeypatch.setattr("admin.helpers_ext._load_secret_plain", fake_load_no_colon)
    assert await _build_room_id_async("ivan", fake_session) == "ivan"
