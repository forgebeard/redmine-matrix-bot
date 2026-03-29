"""
Matrix-клиент: singleton AsyncClient и отправка HTML в комнаты.

Обёртка над nio; отправка через matrix_send.room_send_with_retry.

Когда использовать: код, импортируемый из src/ (тесты, будущие модули).
Корневой bot.py создаёт AsyncClient сам и тоже зовёт room_send_with_retry —
два входа, одна логика повторов в matrix_send.py.
"""

import logging
import re

from nio import AsyncClient

from config import (
    MATRIX_HOMESERVER,
    MATRIX_ACCESS_TOKEN,
    MATRIX_USER_ID,
    MATRIX_DEVICE_ID,
)
from matrix_send import room_send_with_retry, MAX_RETRIES

logger = logging.getLogger("redmine_bot")

# ═══════════════════════════════════════════════════════════════
# КЛИЕНТ
# ═══════════════════════════════════════════════════════════════

_client: AsyncClient | None = None


async def get_client() -> AsyncClient:
    """
    Возвращает подключённый Matrix-клиент (singleton).
    Использует access_token — логин не нужен.
    """
    global _client

    if _client is not None:
        return _client

    client = AsyncClient(MATRIX_HOMESERVER, MATRIX_USER_ID)
    client.access_token = MATRIX_ACCESS_TOKEN
    client.device_id = MATRIX_DEVICE_ID or "BOT"
    client.user_id = MATRIX_USER_ID

    logger.info(f"✅ Matrix: клиент создан для {MATRIX_USER_ID}")
    _client = client
    return client


async def close_client():
    """Закрывает Matrix-клиент."""
    global _client
    if _client:
        await _client.close()
        _client = None
        logger.info("Matrix: клиент закрыт")


async def send_message(room_id: str, html: str, text: str = "") -> bool:
    """
    Отправляет HTML-сообщение в комнату Matrix.

    Args:
        room_id: ID комнаты (!xxx:server)
        html: HTML-тело сообщения
        text: Plaintext fallback (если пусто — strip HTML)

    Returns:
        True при успехе, False при ошибке.
    """
    if not text:
        text = re.sub(r"<[^>]+>", "", html)

    client = await get_client()

    content = {
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": html,
    }

    try:
        await room_send_with_retry(client, room_id, content)
        return True
    except Exception as e:
        logger.error(f"❌ Не удалось отправить в {room_id} после {MAX_RETRIES} попыток: {e}")
        return False
