import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.enums import ParseMode

from src.utils.retry import call_with_flood_retry

logger = logging.getLogger(__name__)

# Small pause between sends so a large recipient list can't trip Telegram's
# per-second flood limits; call_with_flood_retry still backs off on top of
# this if a FloodWait slips through anyway.
_SEND_GAP_SECONDS = 0.05


async def _active_premium_ids(access_control) -> set[int]:
    try:
        records = await access_control.list_premium_records()
    except Exception:
        logger.exception("[Startup] Could not load Premium users for the restart notice.")
        return set()

    now = datetime.now(timezone.utc)
    active: set[int] = set()
    for record in records:
        user_id = record.get("user_id")
        if not isinstance(user_id, int):
            continue
        expires_at = record.get("expires_at")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                continue  # expired - same rule as AccessControl.is_premium
        active.add(user_id)
    return active


async def notify_startup(app: Client, access_control, me=None) -> None:
    """Fire-and-forget: let owners and currently-active Premium users know
    the bot just (re)started.

    Failures for individual recipients (blocked the bot, never started a
    chat with it, deleted account, etc.) are swallowed per-user so one bad
    chat can't stop the rest of the broadcast, and this never raises back
    into the startup sequence.
    """
    recipients = set(access_control.owner_ids) | await _active_premium_ids(access_control)
    if not recipients:
        return

    username = f"@{me.username}" if me and getattr(me, "username", None) else "The bot"
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    text = (
        "<b>▸ Bot Restarted</b>\n"
        "────────────────\n"
        f"{username} just came back online and is ready to use.\n"
        f"<blockquote>{started_at}</blockquote>"
    )

    sent = failed = 0
    for user_id in recipients:
        try:
            await call_with_flood_retry(
                app.send_message,
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.info("[Startup] Restart notice skipped for %s: %s", user_id, exc)
        await asyncio.sleep(_SEND_GAP_SECONDS)

    logger.info(
        "[Startup] Restart notice sent to %d/%d owner(s)/Premium user(s) (%d failed).",
        sent, len(recipients), failed,
    )
