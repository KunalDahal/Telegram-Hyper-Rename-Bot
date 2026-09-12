import os

from pyrogram import Client
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from src.utils.commands import command_filter, chat_scope_filter


def setup_set_handlers(app: Client, user_settings, config, access_control):

    allowed_filter = chat_scope_filter(config)

    @app.on_message(command_filter(config, ["ss", "set_start_episode"]) & allowed_filter)
    async def set_start_episode_command(client: Client, message: Message):
        user_id = message.from_user.id
        if not await access_control.is_authorized(user_id):
            return
        if len(message.command) != 2 or not message.command[1].isdigit():
            await message.reply_text(
                "Usage: <code>/ss &lt;episode&gt;</code>\nExample: <code>/ss 001</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        episode = int(message.command[1])
        if episode < 1:
            await message.reply_text("Episode must be at least <code>1</code>.", parse_mode=ParseMode.HTML)
            return

        user_settings(user_id).update("default_start_episode", message.command[1])
        await message.reply_text(
            f"Start episode set to <code>{message.command[1]}</code>.",
            parse_mode=ParseMode.HTML,
        )

    @app.on_message(command_filter(config, ["st", "setthumb"]) & allowed_filter)
    async def set_thumbnail_command(client: Client, message: Message):
        user_id = message.from_user.id

        if not await access_control.is_authorized(user_id):
            return

        replied = message.reply_to_message
        if not replied:
            await message.reply_text(
                "Reply to a photo with <code>/st2</code> to set it as your thumbnail.",
                parse_mode=ParseMode.HTML,
            )
            return

        is_photo   = bool(replied.photo)
        is_img_doc = (
            replied.document
            and (replied.document.mime_type or "").startswith("image/")
        )

        if not is_photo and not is_img_doc:
            await message.reply_text(
                "The replied message must be a photo or an image file.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            thumb_dir = config.paths.thumbnails
            os.makedirs(thumb_dir, exist_ok=True)
            dest_path = os.path.join(thumb_dir, f"{user_id}.jpg")

            downloaded = await client.download_media(replied, file_name=dest_path)

            if not downloaded or not os.path.exists(downloaded):
                await message.reply_text("Failed to download the image. Please try again.")
                return

            us = user_settings(user_id)
            us.set_thumbnail(os.path.abspath(downloaded))

            await message.reply_text(
                "✅ Thumbnail saved successfully.",
                parse_mode=ParseMode.HTML,
            )

        except Exception as e:
            await message.reply_text(
                f"<b>Error saving thumbnail:</b> <code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
