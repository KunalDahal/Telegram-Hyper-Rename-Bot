from __future__ import annotations

import os
import uuid
import shutil
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from src.utils.commands import command_filter, chat_scope_filter
from src.utils.safe_path import safe_join
from src.utils.telegraphpage import MediaInfoHelper

_telegraph = MediaInfoHelper()

PARTIAL_BYTES = 3 * 1024 * 1024


async def _handle_mi_command(client, message: Message, access_control):
    user = message.from_user
    if not user or not await access_control.can_use_premium_features(user.id):
        return

    replied = message.reply_to_message
    if not replied:
        await message.reply_text(
            "<b>▸ Usage</b>\n"
            "────────────────\n"
            "Reply to a media file with <code>/mi</code> to get its MediaInfo.",
            parse_mode=ParseMode.HTML,
        )
        return

    media = (
        replied.document
        or replied.video
        or replied.audio
        or replied.voice
        or replied.video_note
    )
    if not media:
        await message.reply_text(
            "<b>▸ Unsupported</b>\n"
            "────────────────\n"
            "<i>The replied message contains no supported media.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    filename  = getattr(media, "file_name", None) or f"file_{media.file_id[:8]}"
    tmp_dir   = f"/tmp/mi_{uuid.uuid4().hex}"

    os.makedirs(tmp_dir, exist_ok=True)
    save_path = safe_join(tmp_dir, filename, fallback=f"file_{media.file_id[:8]}")
    status_msg = await message.reply_text(
        "<b>▸ Downloading</b>\n"
        "────────────────\n"
        "<i>Fetching a partial copy of the file…</i>",
        parse_mode=ParseMode.HTML,
    )

    try:
        await _telegraph.download_partial(
            client=client,
            media=media,
            save_path=save_path,
            max_bytes=PARTIAL_BYTES,
        )

        await status_msg.edit_text(
            "<b>▸ Analyzing</b>\n"
            "────────────────\n"
            "<i>Generating the MediaInfo page…</i>",
            parse_mode=ParseMode.HTML,
        )
        url, err = await _telegraph.generate_mediainfo(save_path, filename)

        if err:
            await status_msg.edit_text(
                f"<b>▸ Error</b>\n"
                f"────────────────\n"
                f"<code>{err}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        await status_msg.edit_text(
            f"<b>▸ MediaInfo</b>\n"
            f"────────────────\n"
            f"<b>File</b> → <code>{filename}</code>\n"
            f"<b>Link</b> → {url}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as e:
        await status_msg.edit_text(
            f"<b>▸ Failed</b>\n"
            f"────────────────\n"
            f"<code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def setup_mediainfo_handlers(app, config, access_control) -> None:
    allowed_filter = chat_scope_filter(config)

    @app.on_message(command_filter(config, ["mi"]) & allowed_filter)
    async def mediainfo_command(client, message: Message):
        await _handle_mi_command(client, message, access_control)
