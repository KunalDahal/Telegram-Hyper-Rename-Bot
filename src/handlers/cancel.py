from pyrogram import Client, enums
from pyrogram.types import Message
from src.utils.commands import command_filter, chat_scope_filter

_worker_instance = None


def set_worker_instance(worker):
    global _worker_instance
    _worker_instance = worker


def get_worker_instance():
    return _worker_instance


async def _check_access(client, message: Message) -> bool:
    user_id = message.from_user.id
    try:
        await client.get_chat(user_id)
    except Exception:
        bot_username = (await client.get_me()).username
        await message.reply_text(
            f"<b>▸ Start Required</b>\n"
            f"────────────────\n"
            f"Please start the bot in <u>DM</u> first.\n\n"
            f"<code>@{bot_username}</code> → press <b>Start</b>, then try again.",
            parse_mode=enums.ParseMode.HTML,
        )
        return False
    return True


def setup_cancel_handlers(app: Client, task_queue, config, access_control):

    @app.on_message(command_filter(config, ["cancel", "c"]) & chat_scope_filter(config))
    async def cancel_command(client: Client, message: Message):
        if not await access_control.can_use_premium_features(message.from_user.id):
            return
        if not await _check_access(client, message):
            return

        if len(message.command) < 2:
            await message.reply_text(
                "<b>▸ Usage</b>\n"
                "────────────────\n"
                "<blockquote><code>/cancel &lt;task_id&gt;</code></blockquote>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        task_id_part = message.command[1].strip()

        matching_task_id = None
        for tid in list(task_queue.tasks.keys()):
            if tid.startswith(task_id_part):
                matching_task_id = tid
                break

        task = task_queue.get_task(matching_task_id)
        if not task:
            return

        user_id = message.from_user.id
        is_owner = access_control.is_owner(user_id)
        if not is_owner and task.get("user_id") != user_id:
            await message.reply_text(
                "<b>▸ Access Denied</b>\n"
                "────────────────\n"
                "<i>You can only cancel your own tasks.</i>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        worker = get_worker_instance()
        if not worker:
            await message.reply_text(
                "<b>▸ Unavailable</b>\n"
                "────────────────\n"
                "<i>Worker is not available right now.</i>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        task_status = task.get("status", "")
        if task_status == "queued":
            task_queue.remove_task(matching_task_id, final_status="cancelled")
            await message.reply_text(
                f"<b>▸ Removed From Queue</b>\n"
                f"────────────────\n"
                f"Task <code>{task_id_part}</code> has been <u>cancelled</u>.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        try:
            await worker.cancel_task(matching_task_id)
            await message.reply_text(
                f"<b>▸ Cancelled</b>\n"
                f"────────────────\n"
                f"Task <code>{task_id_part}</code> was <u>successfully cancelled</u>.",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception as e:
            await message.reply_text(
                f"<b>▸ Cancel Failed</b>\n"
                f"────────────────\n"
                f"<code>{e}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
