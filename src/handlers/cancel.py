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
            f"⚠️ Please start the bot in DM first.\n"
            f"👉 @{bot_username} — press <b>Start</b>, then try again.",
            parse_mode=enums.ParseMode.HTML,
        )
        return False
    return True


def setup_cancel_handlers(app: Client, task_queue, config, access_control):

    @app.on_message(command_filter(config, ["cancel", "c"]) & chat_scope_filter(config))
    async def cancel_command(client: Client, message: Message):
        if not await access_control.is_authorized(message.from_user.id):
            return
        if not await _check_access(client, message):
            return

        if len(message.command) < 2:
            await message.reply_text(
                "Usage: <code>/cancel &lt;task_id&gt;</code>\n",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        task_id_part = message.command[1].strip()

        # ── Match task by prefix ──────────────────────────────────────────────
        matching_task_id = None
        for tid in list(task_queue.tasks.keys()):
            if tid.startswith(task_id_part):
                matching_task_id = tid
                break

        task = task_queue.get_task(matching_task_id)
        if not task:
            return

        # ── Ownership check ───────────────────────────────────────────────────
        user_id = message.from_user.id
        is_owner = access_control.is_owner(user_id)
        if not is_owner and task.get("user_id") != user_id:
            await message.reply_text(
                "You can only cancel your own tasks.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        worker = get_worker_instance()
        if not worker:
            await message.reply_text(
                "Worker is not available.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        task_status = task.get("status", "")
        if task_status == "queued":
            task_queue.remove_task(matching_task_id, final_status="cancelled")
            await message.reply_text(
                f"Task <code>{task_id_part}</code> cancelled.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        # ── Active task — delegate to worker ─────────────────────────────────
        try:
            await worker.cancel_task(matching_task_id)
            await message.reply_text(
                f"✅ Task <code>{task_id_part}</code> cancelled.",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception as e:
            await message.reply_text(
                f"Failed to cancel task: <code>{e}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
