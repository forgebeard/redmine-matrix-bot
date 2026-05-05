"""
Единая отправка m.room.message в Matrix: повторы и экспоненциальный backoff.

Используется ботом и админскими маршрутами test-message через общий retry-контур.

Ответ успешной отправки в nio обычно содержит event_id; ошибки — RoomSendError
или ответ с status_code без event_id. В тестах nio подменяют, поэтому проверка
не только через isinstance(RoomSendError).
"""

import asyncio
import logging
from typing import Any

from nio import RoomSendError

logger = logging.getLogger("redmine_bot")


def _log_matrix_send_response(resp, room_id: str, *, prefix: str = "Matrix room_send") -> None:
    """Детальный разбор ответа nio для диагностики M_FORBIDDEN и др."""
    parts = [f"{prefix} room={room_id!r}"]
    for key in ("message", "status_code", "body", "transport_response", "event_id"):
        val = getattr(resp, key, None)
        if val is not None:
            parts.append(f"{key}={val!r}")
    if isinstance(resp, RoomSendError):
        parts.append("type=RoomSendError")
    logger.warning("; ".join(parts))


def _get_retry_settings() -> tuple[int, float]:
    """Читает retry-настройки из config (с fallback)."""
    try:
        from config import MATRIX_RETRY_BASE_DELAY_SEC, MATRIX_RETRY_MAX_ATTEMPTS

        return MATRIX_RETRY_MAX_ATTEMPTS, MATRIX_RETRY_BASE_DELAY_SEC
    except ImportError:
        return 3, 1.0


# Re-export для обратной совместимости (тесты)
MAX_RETRIES, RETRY_BASE_SEC = _get_retry_settings()
RETRY_DELAYS_SEC = (1.0, 3.0, 7.0)


def _retry_delay_for_attempt(attempt: int) -> float:
    idx = max(0, min(attempt - 1, len(RETRY_DELAYS_SEC) - 1))
    return RETRY_DELAYS_SEC[idx]


def _status_int(resp: Any) -> int | None:
    code = getattr(resp, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


async def room_send_with_retry(client, room_id, content, *, txn_id: str | None = None):
    """
    Отправка в комнату с повторными попытками.

    При RoomSendError или исключении сети — warning и пауза до MAX_RETRIES раз.
    Итог: проброс последней ошибки.
    """
    max_retries, retry_base_sec = _get_retry_settings()
    last_err: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
                tx_id=txn_id,
            )
            # Успех: у ответа nio есть event_id (совместимо с моками, где isinstance(RoomSendError) ломается)
            if getattr(resp, "event_id", None):
                return resp
            if isinstance(resp, RoomSendError):
                _log_matrix_send_response(resp, room_id)
                sc_int = _status_int(resp)
                last_err = RuntimeError(
                    f"Matrix room_send error: {getattr(resp, 'message', resp)} "
                    f"(status_code={getattr(resp, 'status_code', None)}, room={room_id})"
                )
                if sc_int is not None and 400 <= sc_int < 500 and sc_int != 429:
                    break
                is_rate_limited = (
                    sc_int == 429
                    or str(getattr(resp, "status_code", "")).upper() == "M_LIMIT_EXCEEDED"
                )
                retry_after_ms = getattr(resp, "retry_after_ms", None)
                if is_rate_limited and retry_after_ms and attempt < max_retries:
                    delay = max(0.1, float(retry_after_ms) / 1000.0)
                    logger.warning(
                        "Matrix rate limited (%s/%s): %s; retry in %.1fs",
                        attempt,
                        max_retries,
                        last_err,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
            elif getattr(resp, "status_code", None) is not None and not getattr(
                resp, "event_id", None
            ):
                _log_matrix_send_response(resp, room_id)
                sc_int = _status_int(resp)
                last_err = RuntimeError(
                    f"Matrix room_send error: {getattr(resp, 'message', resp)} "
                    f"(status_code={resp.status_code}, room={room_id})"
                )
                if sc_int is not None and 400 <= sc_int < 500 and sc_int != 429:
                    break
            else:
                return resp
        except Exception as e:
            last_err = e
            logger.warning(
                "Matrix send exception (%s/%s) room=%s: %s: %s",
                attempt,
                max_retries,
                room_id,
                type(e).__name__,
                e,
            )

        if attempt >= max_retries:
            break

        delay = _retry_delay_for_attempt(attempt)
        logger.warning(
            "Matrix send failed (%s/%s): %s; retry in %.1fs",
            attempt,
            max_retries,
            last_err,
            delay,
        )
        await asyncio.sleep(delay)

    if last_err is not None:
        raise last_err
    raise RuntimeError(f"Matrix room_send failed after {max_retries} attempts (room={room_id})")
