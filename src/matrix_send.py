"""
Единая отправка m.room.message в Matrix: повторы и экспоненциальный backoff.

Используется и корневым bot.py (свой AsyncClient), и matrix_client (singleton).

Ответ успешной отправки в nio обычно содержит event_id; ошибки — RoomSendError
или ответ с status_code без event_id. В тестах nio подменяют, поэтому проверка
не только через isinstance(RoomSendError).
"""

import asyncio
import logging

from nio import RoomSendError

logger = logging.getLogger("redmine_bot")

MAX_RETRIES = 3
RETRY_BASE_SEC = 1.0


async def room_send_with_retry(client, room_id, content):
    """
    Отправка в комнату с повторными попытками.

    При RoomSendError или исключении сети — warning и пауза до MAX_RETRIES раз.
    Итог: проброс последней ошибки.
    """
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.room_send(
                room_id=room_id, message_type="m.room.message", content=content
            )
            # Успех: у ответа nio есть event_id (совместимо с моками, где isinstance(RoomSendError) ломается)
            if getattr(resp, "event_id", None):
                return resp
            if isinstance(resp, RoomSendError):
                last_err = RuntimeError(
                    f"Matrix room_send error: {getattr(resp, 'message', resp)} "
                    f"(status_code={getattr(resp, 'status_code', None)}, room={room_id})"
                )
            elif getattr(resp, "status_code", None) is not None and not getattr(
                resp, "event_id", None
            ):
                last_err = RuntimeError(
                    f"Matrix room_send error: {getattr(resp, 'message', resp)} "
                    f"(status_code={resp.status_code}, room={room_id})"
                )
            else:
                return resp
        except Exception as e:
            last_err = e

        if attempt >= MAX_RETRIES:
            break

        delay = RETRY_BASE_SEC * (2 ** (attempt - 1))
        logger.warning(
            "Matrix send failed (%s/%s): %s; retry in %.1fs",
            attempt,
            MAX_RETRIES,
            last_err,
            delay,
        )
        await asyncio.sleep(delay)

    if last_err is not None:
        raise last_err
    raise RuntimeError(
        f"Matrix room_send failed after {MAX_RETRIES} attempts (room={room_id})"
    )
