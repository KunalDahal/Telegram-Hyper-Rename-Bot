import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from src.core.access_control import AccessControl, WorkerStoreError
from src.utils.commands import command_filter


logger = logging.getLogger(__name__)


def _target_id_from_command(message: Message) -> int | None:
    if len(message.command) < 2:
        return None
    try:
        user_id = int(message.command[1])
    except ValueError:
        return None
    return user_id if user_id > 0 else None


def _ban_reason_from_command(message: Message) -> str:
    return " ".join(message.command[2:]).strip()


def _format_banned_at(banned_at) -> str:
    if banned_at is None:
        return "Unknown"
    return banned_at.strftime("%Y-%m-%d %H:%M UTC")


def _require_owner(message: Message, access_control: AccessControl) -> bool:
    user = message.from_user
    if not user or not access_control.is_owner(user.id):
        return False
    return True


def setup_moderation_handlers(app: Client, config, access_control: AccessControl) -> None:
    allowed_filter = filters.private | filters.chat(config.allowed_group_ids)

    @app.on_message(command_filter(config, ["ban"]) & allowed_filter)
    async def ban_command(client: Client, message: Message):
        if not _require_owner(message, access_control):
            return

        target_id = _target_id_from_command(message)
        if target_id is None:
            await message.reply_text(
                "<b>▸ Usage</b>\n"
                "────────────────\n"
                "<blockquote><code>/ban &lt;user_id&gt; [reason]</code></blockquote>",
                parse_mode=ParseMode.HTML,
            )
            return

        if access_control.is_owner(target_id):
            await message.reply_text(
                "<b>▸ Not Allowed</b>\n"
                "────────────────\n"
                "<i>Bootstrap owners cannot be banned.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        reason = _ban_reason_from_command(message)
        try:
            banned = await access_control.ban_user(target_id, message.from_user.id, reason)
        except WorkerStoreError as exc:
            logger.exception("Unable to ban user")
            await message.reply_text(
                f"<b>▸ Error</b>\n"
                f"────────────────\n"
                f"<code>{exc}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        label = "banned" if banned else "already banned"
        reason_line = f"\n┖ Reason : <code>{reason}</code>" if banned and reason else ""
        await message.reply_text(
            f"<b>▸ Ban Updated</b>\n"
            f"────────────────\n"
            f"┃ User : <code>{target_id}</code>\n"
            f"┖ Status : <u>{label}</u>{reason_line}",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(command_filter(config, ["unban"]) & allowed_filter)
    async def unban_command(client: Client, message: Message):
        if not _require_owner(message, access_control):
            return

        target_id = _target_id_from_command(message)
        if target_id is None:
            await message.reply_text(
                "<b>▸ Usage</b>\n"
                "────────────────\n"
                "<blockquote><code>/unban &lt;user_id&gt;</code></blockquote>",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            unbanned = await access_control.unban_user(target_id)
        except WorkerStoreError as exc:
            logger.exception("Unable to unban user")
            await message.reply_text(
                f"<b>▸ Error</b>\n"
                f"────────────────\n"
                f"<code>{exc}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        label = "unbanned" if unbanned else "was not banned"
        await message.reply_text(
            f"<b>▸ Unban Updated</b>\n"
            f"────────────────\n"
            f"┃ User : <code>{target_id}</code>\n"
            f"┖ Status : <u>{label}</u>",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(command_filter(config, ["banned_users"]) & allowed_filter)
    async def banned_users_command(client: Client, message: Message):
        if not _require_owner(message, access_control):
            return

        try:
            records = await access_control.list_banned()
        except WorkerStoreError as exc:
            logger.exception("Unable to list banned users")
            await message.reply_text(
                f"<b>▸ Error</b>\n"
                f"────────────────\n"
                f"<code>{exc}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        if not records:
            await message.reply_text(
                "<b>▸ Banned Users</b>\n"
                "────────────────\n"
                "<i>No users are currently banned.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        lines = [f"<b>▸ Banned Users</b>  <code>({len(records)})</code>", "────────────────"]
        for record in records:
            user_id = record.get("user_id")
            reason = (record.get("reason") or "").strip()
            banned_at = _format_banned_at(record.get("banned_at"))
            lines.append(f"• <code>{user_id}</code>  —  <i>{banned_at}</i>")
            if reason:
                lines.append(f"  ┖ Reason : <code>{reason}</code>")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3990] + "\n<i>…(truncated)</i>"

        await message.reply_text(text, parse_mode=ParseMode.HTML)
