"""
Тесты src/matrix_client.py: singleton клиента и send_message.

nio подменяется до импорта matrix_client/matrix_send — реального сервера нет.
Проверяем retry и сборку content для m.room.message.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# Подмена nio до импорта: иначе matrix_send подтянет реальный пакет
import sys
mock_nio = MagicMock()
mock_nio.RoomSendError = type("RoomSendError", (), {})
sys.modules["nio"] = mock_nio

from matrix_client import send_message, get_client, close_client, MAX_RETRIES
import matrix_client as mc


@pytest.fixture(autouse=True)
def reset_client():
    """Сбрасываем singleton перед каждым тестом."""
    mc._client = None
    yield
    mc._client = None


# ═══════════════════════════════════════════════════════════════
# get_client
# ═══════════════════════════════════════════════════════════════

class TestGetClient:

    @pytest.mark.asyncio
    async def test_creates_client(self):
        client = await get_client()
        assert client is not None
        assert mc._client is client

    @pytest.mark.asyncio
    async def test_returns_singleton(self):
        c1 = await get_client()
        c2 = await get_client()
        assert c1 is c2


# ═══════════════════════════════════════════════════════════════
# close_client
# ═══════════════════════════════════════════════════════════════

class TestCloseClient:

    @pytest.mark.asyncio
    async def test_close(self):
        mock_client = AsyncMock()
        mc._client = mock_client
        await close_client()
        mock_client.close.assert_called_once()
        assert mc._client is None

    @pytest.mark.asyncio
    async def test_close_when_none(self):
        mc._client = None
        await close_client()  # Не должно падать


# ═══════════════════════════════════════════════════════════════
# send_message
# ═══════════════════════════════════════════════════════════════

class TestSendMessage:

    @pytest.mark.asyncio
    async def test_success(self):
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(return_value="ok")
        mc._client = mock_client

        result = await send_message("!room:test", "<b>Hello</b>")
        assert result is True
        mock_client.room_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_html_in_content(self):
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(return_value="ok")
        mc._client = mock_client

        await send_message("!room:test", "<b>Bold</b>", text="Bold")

        call_kwargs = mock_client.room_send.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content")
        assert content["formatted_body"] == "<b>Bold</b>"
        assert content["body"] == "Bold"

    @pytest.mark.asyncio
    async def test_auto_strip_html_for_text(self):
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(return_value="ok")
        mc._client = mock_client

        await send_message("!room:test", "<b>Hello</b> world")

        call_kwargs = mock_client.room_send.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content")
        assert "<" not in content["body"]

    @pytest.mark.asyncio
    async def test_retry_on_exception(self):
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(
            side_effect=[Exception("fail"), Exception("fail"), "ok"]
        )
        mc._client = mock_client

        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            result = await send_message("!room:test", "test")

        assert result is True
        assert mock_client.room_send.call_count == 3

    @pytest.mark.asyncio
    async def test_all_retries_fail(self):
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(side_effect=Exception("fail"))
        mc._client = mock_client

        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            result = await send_message("!room:test", "test")

        assert result is False
        assert mock_client.room_send.call_count == MAX_RETRIES

    @pytest.mark.asyncio
    async def test_room_send_error_retries(self):
        error_resp = mock_nio.RoomSendError()
        error_resp.message = "temporary"
        error_resp.status_code = 429
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(
            side_effect=[error_resp, "ok"]
        )
        mc._client = mock_client

        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            result = await send_message("!room:test", "test")

        assert result is True
        assert mock_client.room_send.call_count == 2