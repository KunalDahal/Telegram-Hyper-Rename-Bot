
import asyncio
import os
import sys

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from src.handlers.cancel import get_worker_instance
from src.utils.commands import command_filter, chat_scope_filter


_restart_scheduled = False


def setup_restart_handler(app: Client, task_queue, config, access_control) -> None:
    allowed_filter = chat_scope_filter(config)

    @app.on_message(command_filter(config, ["restart"]) & allowed_filter)
    async def restart_command(client: Client, message: Message):
        global _restart_scheduled
        user = message.from_user
        if not user or not access_control.is_owner(user.id):
            return
        if _restart_scheduled:
            await message.reply_text(
                "<b>▸ Restart In Progress</b>\n"
                "────────────────\n"
                "<i>A clean restart is already running.</i>"
            )
            return

        _restart_scheduled = True
        await message.reply_text(
            "<b>▸ Restarting</b>\n"
            "────────────────\n"
            "Cancelling tasks, clearing task records and temporary files.\n\n"
            "<blockquote><u>Preserved:</u> admin records and the login session.</blockquote>",
            parse_mode=ParseMode.HTML,
        )

        async def restart_process() -> None:
            await asyncio.sleep(0.5)
            worker = get_worker_instance()
            if worker:
                await worker.clear_for_restart()
            else:
                task_queue.clear()

            await task_queue.flush_checkpoints()
            await access_control.task_store.clear_all()

            await app.stop()
            os.execv(sys.executable, [sys.executable, *sys.argv])

        asyncio.create_task(restart_process())
