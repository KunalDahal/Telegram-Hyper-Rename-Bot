"""Helpers for applying the configured suffix to every Telegram command."""

from pyrogram import filters


def command_filter(config, names: list[str]):
    """Build a Pyrogram command filter with the configured numeric postfix."""
    return filters.command([f"{name}{config.command_postfix}" for name in names])


def command_text(config, name: str) -> str:
    """Return a display-ready command, for example ``/rename2``."""
    return f"/{name}{config.command_postfix}"


def chat_scope_filter(config):
    """Filter matching DMs plus any configured allowed group chats.

    Every command handler should use this instead of composing its own
    ``filters.private | filters.chat(...)`` expression, so DM support stays
    consistent everywhere and ALLOWED_GROUP_IDS can be left empty for a
    DM-only deployment.
    """
    if config.allowed_group_ids:
        return filters.private | filters.chat(config.allowed_group_ids)
    return filters.private


def chat_in_scope(config, chat_id: int, user_id: int | None = None) -> bool:
    """True if ``chat_id`` is an allowed group OR the caller's own DM."""
    if chat_id in config.allowed_group_ids:
        return True
    return user_id is not None and chat_id == user_id


def group_scope_filter(config):
    """Filter matching ONLY the configured allowed group chats (no DMs).

    Use this instead of ``chat_scope_filter`` for commands that must never
    run in a private chat (e.g. /rename and /status). If ALLOWED_GROUP_IDS
    is empty, the command simply won't match anywhere until it's configured.
    """
    if not config.allowed_group_ids:
        return filters.create(lambda _, __, ___: False)
    return filters.chat(config.allowed_group_ids)


def chat_in_group_scope(config, chat_id: int) -> bool:
    """True if ``chat_id`` is one of the configured allowed group chats.

    Unlike ``chat_in_scope`` this deliberately excludes DMs, for callbacks
    that belong to group-only commands (e.g. /status buttons).
    """
    return chat_id in config.allowed_group_ids
