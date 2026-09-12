from __future__ import annotations

import os
import uuid
import shutil
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from src.utils.commands import command_filter, chat_scope_filter
from src.utils.telegraphpage import MediaInfoHelper

# ─── Singleton ────────────────────────────────────────────────────────────────
_telegraph = MediaInfoHelper()

PARTIAL_BYTES = 3 * 1024 * 1024


# ─── Handler ──────────────────────────────────────────────────────────────────

async def _handle_mi_command(client, message: Message, access_control):
    user = message.from_user
    if not user or not await access_control.is_authorized(user.id):
        return

    replied = message.reply_to_message
    if not replied:
        await message.reply_text("⚠️ Reply to a media file with /mi to get its MediaInfo.")
        return

    media = (
        replied.document
        or replied.video
        or replied.audio
        or replied.voice
        or replied.video_note
    )
    if not media:
        await message.reply_text("⚠️ The replied message contains no supported media.")
        return

    filename  = getattr(media, "file_name", None) or f"file_{media.file_id[:8]}"
    tmp_dir   = f"/tmp/mi_{uuid.uuid4().hex}"
    save_path = os.path.join(tmp_dir, filename)

    os.makedirs(tmp_dir, exist_ok=True)
    status_msg = await message.reply_text("⏳ Downloading partial file…")

    try:
        # ── 1. Partial download (head only) ───────────────────────────────────
        await _telegraph.download_partial(
            client=client,
            media=media,
            save_path=save_path,
            max_bytes=PARTIAL_BYTES,
        )

        # ── 2. Generate Telegraph page ────────────────────────────────────────
        await status_msg.edit_text("🔍 Generating MediaInfo page…")
        url, err = await _telegraph.generate_mediainfo(save_path, filename)

        if err:
            await status_msg.edit_text(f"❌ Error: {err}")
            return

        # ── 3. Reply with link ────────────────────────────────────────────────
        await status_msg.edit_text(
            f"📄 <b>MediaInfo</b>\n"
            f"<b>File:</b> <code>{filename}</code>\n"
            f"<b>Link:</b> {url}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Failed: {e}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Register ─────────────────────────────────────────────────────────────────

def setup_mediainfo_handlers(app, config, access_control) -> None:
    allowed_filter = chat_scope_filter(config)

    @app.on_message(command_filter(config, ["mi"]) & allowed_filter)
    async def mediainfo_command(client, message: Message):
        await _handle_mi_command(client, message, access_control)
