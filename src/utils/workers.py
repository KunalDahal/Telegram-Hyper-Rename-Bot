import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Message,
)

from src.core.access_control import AccessControl, WorkerStoreError
from src.utils.commands import command_filter


logger = logging.getLogger(__name__)


def _command(config, name: str) -> str:
    return f"{name}{config.command_postfix}"


def _normal_commands(config) -> list[BotCommand]:
    return [
        BotCommand(_command(config, "start"), "Open the bot help"),
        BotCommand(_command(config, "rename"), "Rename a video"),
        BotCommand(_command(config, "es"), "Open settings"),
        BotCommand(_command(config, "ss"), "Set the starting episode"),
        BotCommand(_command(config, "st"), "Set the output thumbnail"),
        BotCommand(_command(config, "status"), "Show task queue status"),
        BotCommand(_command(config, "cancel"), "Cancel a task"),
        BotCommand(_command(config, "mi"), "Generate MediaInfo"),
    ]


def _admin_commands(config) -> list[BotCommand]:
    commands = _normal_commands(config)
    commands.append(BotCommand(_command(config, "restart"), "Restart the bot"))
    return commands


def _owner_commands(config) -> list[BotCommand]:
    commands = _admin_commands(config)
    commands.extend([
        BotCommand(_command(config, "add_admin"), "Add a MongoDB administrator"),
        BotCommand(_command(config, "remove_admin"), "Remove a MongoDB administrator"),
        BotCommand(_command(config, "list_admin"), "List MongoDB administrators"),
    ])
    return commands


async def set_user_command_scope(client: Client, config, user_id: int, role: str) -> None:
    """Set the private-chat command menu for one bot user."""
    if role == "owner":
        commands = _owner_commands(config)
    elif role == "admin":
        commands = _admin_commands(config)
    else:
        commands = _normal_commands(config)

    await client.set_bot_commands(
        commands,
        scope=BotCommandScopeChat(chat_id=user_id),
    )


async def sync_bot_command_scopes(client: Client, config, access_control: AccessControl) -> None:
    """Synchronize the default menu and all MongoDB-backed admin/owner menus."""
    await client.set_bot_commands(
        _normal_commands(config),
        scope=BotCommandScopeDefault(),
    )

    admin_ids = await access_control.list_admins()
    for user_id in admin_ids:
        role = "owner" if access_control.is_owner(user_id) else "admin"
        try:
            await set_user_command_scope(client, config, user_id, role)
        except Exception:
            logger.warning(
                "Could not set command scope for user %s", user_id, exc_info=True
            )


def _admin_id_from_command(message: Message) -> int | None:
    if len(message.command) != 2:
        return None
    try:
        admin_id = int(message.command[1])
    except ValueError:
        return None
    return admin_id if admin_id > 0 else None


async def _require_owner(message: Message, access_control: AccessControl) -> bool:
    user = message.from_user
    if not user or not access_control.is_owner(user.id):
        return False
    return True


def setup_worker_handlers(app: Client, config, access_control: AccessControl) -> None:
    """Register MongoDB administrator commands; legacy worker aliases remain valid."""
    allowed_filter = filters.private | filters.chat(config.allowed_group_ids)

    @app.on_message(command_filter(config, ["add_admin", "add_workers"]) & allowed_filter)
    async def add_admin_command(client: Client, message: Message):
        if not await _require_owner(message, access_control):
            return
        admin_id = _admin_id_from_command(message)
        if admin_id is None:
            await message.reply_text("Usage: <code>/add_admin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
            return
        if access_control.is_owner(admin_id):
            await message.reply_text("That ID is already a bootstrap owner.", parse_mode=ParseMode.HTML)
            return
        try:
            added = await access_control.add_admin(admin_id, message.from_user.id)
        except WorkerStoreError as exc:
            logger.exception("Unable to add administrator")
            await message.reply_text(f"Error: {exc}")
            return
        try:
            await set_user_command_scope(client, config, admin_id, "admin")
        except Exception:
            logger.warning("Admin was added, but its Telegram command scope could not be set", exc_info=True)
        label = "added" if added else "already exists"
        await message.reply_text(f"Admin <code>{admin_id}</code> {label}.", parse_mode=ParseMode.HTML)

    @app.on_message(command_filter(config, ["remove_admin", "remove_workers"]) & allowed_filter)
    async def remove_admin_command(client: Client, message: Message):
        if not await _require_owner(message, access_control):
            return
        admin_id = _admin_id_from_command(message)
        if admin_id is None:
            await message.reply_text("Usage: <code>/remove_admin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
            return
        if access_control.is_owner(admin_id):
            await message.reply_text("Bootstrap owners in <code>OWNER_IDS</code> cannot be removed.", parse_mode=ParseMode.HTML)
            return
        try:
            removed = await access_control.remove_admin(admin_id)
        except WorkerStoreError as exc:
            logger.exception("Unable to remove administrator")
            await message.reply_text(f"Error: {exc}")
            return
        if removed:
            try:
                await set_user_command_scope(client, config, admin_id, "user")
            except Exception:
                logger.warning("Admin was removed, but its Telegram command scope could not be reset", exc_info=True)
        label = "removed" if removed else "was not found"
        await message.reply_text(f"Admin <code>{admin_id}</code> {label}.", parse_mode=ParseMode.HTML)

    @app.on_message(command_filter(config, ["list_admin", "view_workers"]) & allowed_filter)
    async def list_admin_command(client: Client, message: Message):
        if not await _require_owner(message, access_control):
            return
        try:
            admin_ids = await access_control.list_admins()
        except WorkerStoreError as exc:
            logger.exception("Unable to list administrators")
            await message.reply_text(f"Error: {exc}")
            return
        if not admin_ids:
            await message.reply_text("No administrators have been added.")
            return
        lines = ["<b>Administrators</b>", ""]
        lines.extend(f"- <code>{admin_id}</code>" for admin_id in admin_ids)
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
