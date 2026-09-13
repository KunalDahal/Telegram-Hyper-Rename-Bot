import logging

from pyrogram import Client
from pyrogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from src.core.access_control import AccessControl


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
        BotCommand(_command(config, "plans"), "See available Premium plans"),
        BotCommand(_command(config, "myplan"), "Check your Premium status"),
    ]


def _owner_commands(config) -> list[BotCommand]:
    commands = _normal_commands(config)
    commands.extend([
        BotCommand(_command(config, "restart"), "[Sudo] Restart the bot"),
        BotCommand(_command(config, "addpremium"), "[Sudo] Grant a user Premium access"),
        BotCommand(_command(config, "remove_premium"), "[Sudo] Revoke a user's Premium access"),
        BotCommand(_command(config, "premium_users"), "[Sudo] List all Premium users"),
        BotCommand(_command(config, "ban"), "[Sudo] Ban a user from the bot"),
        BotCommand(_command(config, "unban"), "[Sudo] Unban a user"),
        BotCommand(_command(config, "banned_users"), "[Sudo] List all banned users"),
    ])
    return commands


async def set_user_command_scope(client: Client, config, user_id: int, role: str) -> None:
    commands = _owner_commands(config) if role == "owner" else _normal_commands(config)
    await client.set_bot_commands(
        commands,
        scope=BotCommandScopeChat(chat_id=user_id),
    )


async def sync_bot_command_scopes(client: Client, config, access_control: AccessControl) -> None:
    await client.set_bot_commands(
        _normal_commands(config),
        scope=BotCommandScopeDefault(),
    )

    for owner_id in access_control.owner_ids:
        try:
            await set_user_command_scope(client, config, owner_id, "owner")
        except Exception:
            logger.warning(
                "Could not set command scope for owner %s", owner_id, exc_info=True
            )
