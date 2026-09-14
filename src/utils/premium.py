import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.core.access_control import AccessControl, WorkerStoreError
from src.utils.commands import command_filter


logger = logging.getLogger(__name__)


PREMIUM_PLANS = ["1 Month", "Lifetime"]

PLANS_IMAGE_URL = "https://i.ibb.co/SDDV4fpw/download.jpg"
DEVELOPER_URL = "https://t.me/NyxyRen"

PREMIUM_FEATURES = [
    "Rename unlimited files",
    "Metadata",
    "Batch rename",
    "Media info",
    "Download / Upload speed: 20~100 Mbps",
    "Custom captions",
]


def _plans_caption() -> str:
    lines = ["<b>▸ Premium Plans</b>", "────────────────", "Plans available, you can avail:"]
    for plan in PREMIUM_PLANS:
        lines.append(f"• <b>{plan}</b>")
    lines.append("")
    lines.append("<b>▸ Features You Get</b>")
    for feature in PREMIUM_FEATURES:
        lines.append(f"• {feature}")
    lines.append("")
    lines.append("<blockquote><i>For pricing, reach out to the developer.</i></blockquote>")
    return "\n".join(lines)


def _plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("▸ Developer", url=DEVELOPER_URL)]])


def _plan_label(record: dict) -> str:
    plan_text = (record.get("plan") or "").strip().lower()
    if "life" in plan_text:
        return "Lifetime"
    if "month" in plan_text:
        return "1 Month"
    return "Lifetime" if record.get("expires_at") is None else "1 Month"


def _format_expiry(expires_at) -> str:
    if expires_at is None:
        return "Never (lifetime)"
    return expires_at.strftime("%Y-%m-%d %H:%M UTC")


def _premium_id_from_command(message: Message) -> int | None:
    if len(message.command) < 2:
        return None
    try:
        user_id = int(message.command[1])
    except ValueError:
        return None
    return user_id if user_id > 0 else None


def _parse_add_premium_args(message: Message) -> tuple[int, str, int | None] | None:
    user_id = _premium_id_from_command(message)
    if user_id is None:
        return None

    args = message.command[2:]
    plan = args[0] if args else "Standard"

    duration_days = None
    if len(args) >= 2:
        try:
            duration_days = int(args[1])
        except ValueError:
            return None
        if duration_days <= 0:
            return None

    return user_id, plan, duration_days


def _require_owner(message: Message, access_control: AccessControl) -> bool:
    user = message.from_user
    if not user or not access_control.is_owner(user.id):
        return False
    return True


def setup_premium_handlers(app: Client, config, access_control: AccessControl) -> None:
    allowed_filter = filters.private | filters.chat(config.allowed_group_ids)

    @app.on_message(command_filter(config, ["addpremium"]) & allowed_filter)
    async def add_premium_command(client: Client, message: Message):
        if not _require_owner(message, access_control):
            return
        parsed = _parse_add_premium_args(message)
        if parsed is None:
            await message.reply_text(
                "<b>▸ Usage</b>\n"
                "────────────────\n"
                "<blockquote><code>/addpremium &lt;user_id&gt; [plan] [duration_days]</code></blockquote>\n"
                "Plan: <code>1 Month</code> or <code>Lifetime</code> "
                "(leave duration_days out, or 0, for Lifetime).",
                parse_mode=ParseMode.HTML,
            )
            return
        user_id, plan, duration_days = parsed
        try:
            added = await access_control.add_premium(
                user_id, message.from_user.id, plan=plan, duration_days=duration_days
            )
        except WorkerStoreError as exc:
            logger.exception("Unable to add Premium user")
            await message.reply_text(
                f"<b>▸ Error</b>\n"
                f"────────────────\n"
                f"<code>{exc}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        label = "added" if added else "already has Premium"
        await message.reply_text(
            f"<b>▸ Premium Updated</b>\n"
            f"────────────────\n"
            f"┃ User : <code>{user_id}</code>\n"
            f"┖ Status : <u>{label}</u>",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(command_filter(config, ["remove_premium"]) & allowed_filter)
    async def remove_premium_command(client: Client, message: Message):
        if not _require_owner(message, access_control):
            return
        user_id = _premium_id_from_command(message)
        if user_id is None:
            await message.reply_text(
                "<b>▸ Usage</b>\n"
                "────────────────\n"
                "<blockquote><code>/remove_premium &lt;user_id&gt;</code></blockquote>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            removed = await access_control.remove_premium(user_id)
        except WorkerStoreError as exc:
            logger.exception("Unable to remove Premium user")
            await message.reply_text(
                f"<b>▸ Error</b>\n"
                f"────────────────\n"
                f"<code>{exc}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        label = "removed" if removed else "was not found"
        await message.reply_text(
            f"<b>▸ Premium Updated</b>\n"
            f"────────────────\n"
            f"┃ User : <code>{user_id}</code>\n"
            f"┖ Status : <u>{label}</u>",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(command_filter(config, ["premium_users"]) & allowed_filter)
    async def premium_users_command(client: Client, message: Message):
        if not _require_owner(message, access_control):
            return
        try:
            records = await access_control.list_premium_records()
        except WorkerStoreError as exc:
            logger.exception("Unable to list Premium users")
            await message.reply_text(
                f"<b>▸ Error</b>\n"
                f"────────────────\n"
                f"<code>{exc}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        if not records:
            await message.reply_text(
                "<b>▸ Premium Users</b>\n"
                "────────────────\n"
                "<i>No Premium users yet.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        lines = [f"<b>▸ Premium Users</b>  <code>({len(records)})</code>", "────────────────"]
        for record in records:
            user_id = record.get("user_id")
            plan = record.get("plan", "Standard")
            expiry = _format_expiry(record.get("expires_at"))
            lines.append(f"• <code>{user_id}</code>  —  <i>{plan}</i>  (expires: <code>{expiry}</code>)")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3990] + "\n<i>…(truncated)</i>"

        await message.reply_text(text, parse_mode=ParseMode.HTML)

    @app.on_message(command_filter(config, ["plans"]) & allowed_filter)
    async def plans_command(client: Client, message: Message):
        await message.reply_photo(
            PLANS_IMAGE_URL,
            caption=_plans_caption(),
            reply_markup=_plans_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(command_filter(config, ["myplan"]) & allowed_filter)
    async def myplan_command(client: Client, message: Message):
        user = message.from_user
        if not user:
            return
        user_id = user.id

        ban_record = await access_control.get_ban(user_id)
        if ban_record:
            lines = ["<b>▸ Banned</b>", "────────────────", "<i>You are banned from using this bot.</i>"]
            reason = (ban_record.get("reason") or "").strip()
            if reason:
                lines.append(f"┖ Reason : <code>{reason}</code>")
            lines.append("")
            lines.append("<blockquote>Contact an admin if you believe this is a mistake.</blockquote>")
            await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        if access_control.is_owner(user_id):
            await message.reply_text(
                "<b>▸ Welcome, Master!</b>\n"
                "────────────────\n"
                "You are the <u>owner</u> of this bot.",
                parse_mode=ParseMode.HTML,
            )
            return

        record = await access_control.get_premium(user_id)
        active = await access_control.is_premium(user_id)

        if active and record:
            plan = _plan_label(record)
            lines = ["<b>▸ Your Premium Status</b>", "────────────────", "┃ Premium : <u>Active</u>", f"┠ Plan : <code>{plan}</code>"]
            if plan == "Lifetime":
                lines.append("┖ Expires : <code>Never (Lifetime)</code>")
            else:
                lines.append(f"┖ Expires : <code>{_format_expiry(record.get('expires_at'))}</code>")
            await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        lines = [
            "<b>▸ Your Premium Status</b>",
            "────────────────",
            "<i>No active plan</i>",
            "",
            "<blockquote>Use <code>/plans</code> to see available plans, then contact the developer to upgrade.</blockquote>",
        ]
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
