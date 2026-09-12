"""Shared Telegram-call retry helper.

Several modules (worker.py, uploader.py, rename.py handlers) each implemented
their own "catch FloodWait, sleep, retry" loop with slightly different retry
caps and logging. This consolidates that pattern into one place, and also
covers Telegram's own transient server-side errors (e.g. the
"500 INTERDC_X_CALL_ERROR" you get when one Telegram datacenter fails to
reach another mid-request) which previously weren't retried at all — those
aren't rate-limiting, they're Telegram's backend having a bad moment, and a
short backoff-and-retry clears them almost every time.
"""

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from pyrogram.errors import FloodWait, RPCError

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_FLOOD_RETRIES = 5
DEFAULT_MAX_TRANSIENT_RETRIES = 4
_TRANSIENT_BACKOFF_BASE = 2  # seconds; doubles each attempt, capped below
_TRANSIENT_BACKOFF_CAP = 20


def _is_transient_rpc_error(exc: Exception) -> bool:
    """Telegram-side 5xx errors (e.g. INTERDC_X_CALL_ERROR, Timeout,
    InternalServerError) — worth a short retry. Anything else (invalid
    peer, permission denied, bad request, etc.) is a real failure and
    should NOT be retried, since retrying those just wastes time and, in
    the case of already-executed calls like copy_message, risks duplicate
    side effects.
    """
    if isinstance(exc, RPCError):
        code = getattr(exc, "CODE", None) or getattr(exc, "code", None)
        return code == 500
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


async def call_with_flood_retry(
    func: Callable[..., Awaitable[T]],
    *args,
    max_retries: int = DEFAULT_MAX_FLOOD_RETRIES,
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES,
    on_wait: Callable[[int, int], None] | None = None,
    **kwargs,
) -> T:
    """Call ``func(*args, **kwargs)``, retrying on FloodWait and on
    transient Telegram-side server errors.

    ``on_wait(delay, attempt)`` is called (if provided) right before
    sleeping for a FloodWait, so callers can update status/progress fields
    without duplicating the loop. Raises the last error once the relevant
    retry cap is exceeded, or immediately for any non-transient error.
    """
    flood_attempt = 0
    transient_attempt = 0
    while True:
        try:
            return await func(*args, **kwargs)
        except FloodWait as exc:
            flood_attempt += 1
            if flood_attempt > max_retries:
                raise
            delay = max(int(getattr(exc, "value", 0) or 0), 1)
            logger.warning(
                "FloodWait %ss (attempt %d/%d) for %s",
                delay, flood_attempt, max_retries, getattr(func, "__name__", func),
            )
            if on_wait:
                on_wait(delay, flood_attempt)
            await asyncio.sleep(delay)
        except Exception as exc:
            if not _is_transient_rpc_error(exc):
                raise
            transient_attempt += 1
            if transient_attempt > max_transient_retries:
                raise
            delay = min(
                _TRANSIENT_BACKOFF_BASE * (2 ** (transient_attempt - 1)),
                _TRANSIENT_BACKOFF_CAP,
            )
            logger.warning(
                "Transient Telegram error (attempt %d/%d) for %s: %s; "
                "retrying in %ss.",
                transient_attempt, max_transient_retries,
                getattr(func, "__name__", func), exc, delay,
            )
            await asyncio.sleep(delay)
