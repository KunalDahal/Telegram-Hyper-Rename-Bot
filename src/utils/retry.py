
import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from pyrogram.errors import FloodWait, RPCError

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_FLOOD_RETRIES = 5
DEFAULT_MAX_TRANSIENT_RETRIES = 4
_TRANSIENT_BACKOFF_BASE = 2  
_TRANSIENT_BACKOFF_CAP = 20


def _is_transient_rpc_error(exc: Exception) -> bool:
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
