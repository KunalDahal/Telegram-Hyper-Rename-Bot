
from pyrogram import filters


def command_filter(config, names: list[str]):
    return filters.command([f"{name}{config.command_postfix}" for name in names])


def command_text(config, name: str) -> str:
    return f"/{name}{config.command_postfix}"


def chat_scope_filter(config):
    if config.allowed_group_ids:
        return filters.private | filters.chat(config.allowed_group_ids)
    return filters.private


def chat_in_scope(config, chat_id: int, user_id: int | None = None) -> bool:
    if chat_id in config.allowed_group_ids:
        return True
    return user_id is not None and chat_id == user_id


def group_scope_filter(config):
    if not config.allowed_group_ids:
        return filters.create(lambda _, __, ___: False)
    return filters.chat(config.allowed_group_ids)


def chat_in_group_scope(config, chat_id: int) -> bool:
    return chat_id in config.allowed_group_ids
