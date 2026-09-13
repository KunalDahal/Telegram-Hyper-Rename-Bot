import copy
import os
import re
import shutil
import uuid
from datetime import datetime
from html import escape

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message
import logging

from src.utils.dc_checker import is_dc_allowed
from src.utils.commands import command_filter, group_scope_filter
from src.utils.retry import call_with_flood_retry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALLOWED_VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi",
    ".mpeg", ".mpg", ".wmv", ".flv", ".3gp",
}

VIDEO_MIME_EXTENSIONS = {
    "video/3gpp": ".3gp",
    "video/avi": ".avi",
    "video/msvideo": ".avi",
    "video/x-msvideo": ".avi",
    "video/x-flv": ".flv",
    "video/x-matroska": ".mkv",
    "video/x-mkv": ".mkv",
    "video/quicktime": ".mov",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpeg",
    "video/webm": ".webm",
    "video/x-ms-wmv": ".wmv",
    "application/mkv": ".mkv",
    "application/x-matroska": ".mkv",
}

_SUPPORTED_PLACEHOLDERS = {"episode"}
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


async def _safe_edit(msg, text: str):
    try:
        await call_with_flood_retry(msg.edit_text, text, parse_mode=ParseMode.HTML)
    except MessageNotModified:
        pass
    except Exception:
        pass


async def fetch_media_group(client: Client, chat_id: int, replied: Message) -> list:
    media_group_id = replied.media_group_id
    start_id = max(1, replied.id - 3)
    end_id   = replied.id + 20
    ids      = list(range(start_id, end_id + 1))
    try:
        messages = await client.get_messages(chat_id, ids)
    except Exception as exc:
        logger.error(f"[rename] fetch_media_group error: {exc}")
        return []
    group = [
        m for m in messages
        if m
        and not getattr(m, "empty", True)
        and m.media_group_id == media_group_id
        and (m.video or m.document)
    ]
    group.sort(key=lambda m: m.id)
    return group


async def fetch_sequential_messages(
    client: Client, chat_id: int, start_id: int, count: int
) -> list:
    ids = list(range(start_id, start_id + count))
    try:
        messages = await client.get_messages(chat_id, ids)
    except Exception as exc:
        logger.error(f"[rename] fetch_sequential_messages error: {exc}")
        return []
    result = [
        m for m in messages
        if m and not getattr(m, "empty", True) and (m.video or m.document)
    ]
    result.sort(key=lambda m: m.id)
    return result


async def _check_access(client, message: Message, access_control) -> bool:
    user_id = message.from_user.id
    if not await access_control.can_use_premium_features(user_id):
        return False
    try:
        await client.get_chat(user_id)
    except Exception:
        await message.reply_text(
            "<b>▸ Start Required</b>\n"
            "────────────────\n"
            "Please start the bot in <u>DM</u> before giving tasks here.",
            parse_mode=ParseMode.HTML,
        )
        return False
    return True


def _valid_extension(filename: str) -> bool:
    if not filename:
        return False
    ext = os.path.splitext(filename.strip())[1].lower()
    return bool(ext) and ext in ALLOWED_VIDEO_EXTENSIONS


def _short_file_token(media) -> str:
    token = (
        getattr(media, "file_unique_id", None)
        or getattr(media, "file_id", None)
        or "unknown"
    )
    return str(token)[:8]


def _document_video_extension(document) -> str:
    filename = (getattr(document, "file_name", None) or "").strip()
    ext = os.path.splitext(filename)[1].lower()
    if ext in ALLOWED_VIDEO_EXTENSIONS:
        return ext

    mime_type = (getattr(document, "mime_type", None) or "").lower().strip()
    if mime_type in VIDEO_MIME_EXTENSIONS:
        return VIDEO_MIME_EXTENSIONS[mime_type]
    if mime_type.startswith("video/"):
        return ".mkv"

    return ""


def _file_info(media_msg: Message):
    if media_msg.video:
        v = media_msg.video
        filename = (v.file_name or "").strip()
        return (
            v.file_id,
            filename or f"video_{_short_file_token(v)}.mp4",
            v.file_size,
        )
    d = media_msg.document
    filename = (d.file_name or "").strip()
    ext = _document_video_extension(d) or ".mkv"
    return (
        d.file_id,
        filename or f"video_{_short_file_token(d)}{ext}",
        d.file_size,
    )


def _source_thumbnail_file_id(msg: Message) -> str:
    media = msg.video or msg.document
    thumbs = getattr(media, "thumbs", None) or []
    if not thumbs:
        return ""
    return getattr(thumbs[-1], "file_id", "") or ""


def _is_video_message(msg: Message) -> bool:
    if msg.video:
        return True
    if msg.document:
        return bool(_document_video_extension(msg.document))
    return False


def _media_debug_label(msg: Message) -> str:
    media = msg.video or msg.document
    if not media:
        return "no video/document media"

    filename = (getattr(media, "file_name", None) or "").strip() or "no filename"
    mime_type = (getattr(media, "mime_type", None) or "").strip() or "no mime"
    media_kind = "video" if msg.video else "document"
    return f"{media_kind}, <code>{escape(filename)}</code>, <code>{escape(mime_type)}</code>"


def _parse_rename_command(message):
    message_text = message.text or ""
    text = re.sub(r"^/\S+\s*", "", message_text).strip()

    is_batch    = False
    batch_count = None  

    if text.startswith("-b"):
        rest = text[2:]  
        num_match = re.match(r"^\s+(\d+)\s*(.*)", rest, re.DOTALL)
        if num_match:
            batch_count = int(num_match.group(1))
            if batch_count < 2:
                return False, None, None, "<code>-b &lt;N&gt;</code> requires N ≥ 2."
            if batch_count > 150:
                return False, None, None, "Only 150 tasks can be batch renamed at a time. Please reply with <code>-b 150</code> or lower."
            rest = num_match.group(2).strip()
        else:
            rest = rest.lstrip()

        if not rest:
            return True, None, None, "Please provide a filename template after <code>-b</code>."

        is_batch = True
        text = rest

    if not text:
        if is_batch:
            return is_batch, batch_count, None, "Please provide a filename template after <code>-b</code>."
        return is_batch, batch_count, None, None
    if (text.startswith('"') and text.endswith('"')) or \
       (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]

    if not text:
        return is_batch, batch_count, None, "Filename cannot be empty."

    return is_batch, batch_count, text, None


def _validate_batch_template(template: str):
    found = {m.group(1) for m in _PLACEHOLDER_RE.finditer(template)}
    unsupported = found - _SUPPORTED_PLACEHOLDERS
    if unsupported:
        bad = ", ".join(f"<code>{{{p}}}</code>" for p in sorted(unsupported))
        return False, (
            f"Unsupported placeholder(s): {bad}\n"
            "Only <code>{episode}</code> is allowed in batch rename filenames."
        )
    return True, None


def _resolve_template(template: str, ep_str: str) -> str:
    filename = template
    filename = filename.replace("{episode}", ep_str)
    return filename


def _primary_username(chat) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return username
    for entry in getattr(chat, "usernames", None) or []:
        candidate = getattr(entry, "username", None)
        if candidate:
            return candidate
    return None


def _build_task(
    *,
    task_id: str,
    message: Message,
    source_message: Message,
    file_id: str,
    original_file_name: str,
    file_size: int,
    source_thumbnail_file_id: str,
    output_filename: str,
    settings: dict,
    watermark: dict,
    caption_template: str,
    created_at: str,
    batch: bool = False,
) -> dict:
    # `settings`/`watermark` are expected to already be a per-task deep copy with
    # any asset paths frozen (see `_freeze_task_assets`, which must run before this
    # is called). We deep-copy once more here purely for isolation/defense-in-depth,
    # not because anything upstream still needs freezing.
    settings_snapshot = copy.deepcopy(settings)
    watermark_snapshot = copy.deepcopy(watermark)

    # `task_assets` holds the concrete, task-owned files this task depends on
    # (already frozen onto disk by `_freeze_task_assets`, called before this
    # function). `settings_snapshot`/`watermark` keep only the *configuration*
    # the user had selected (auto-thumbnail flag, watermark styling, etc.).
    # Previously the same frozen thumbnail path was copied into three places
    # (top-level task, settings_snapshot, and the job dict) with no single
    # place actually treated as authoritative afterwards - just an `or` chain
    # of fallbacks hoping they stayed in sync. Keeping one asset block avoids
    # that duplication and makes "what settings vs. what files" unambiguous.
    task_assets = {
        "thumbnail_path":      settings_snapshot.pop("thumbnail_path", "") or "",
        "watermark_font_path": watermark_snapshot.pop("font_path", "") or "",
    }

    job = {
        "resolution":      "rename",
        "output_filename": output_filename,
        "processing_mode": "rename",
        "audio_bitrate":   None,
        "metadata":        copy.deepcopy(settings_snapshot.get("metadata", {})),
        "send_type":       settings_snapshot.get("send_type", "media"),
    }
    return {
        "task_id":                   task_id,
        "user_id":                   message.from_user.id,
        "first_name":                message.from_user.first_name,
        "username":                  message.from_user.username,
        "chat_id":                   message.chat.id,
        "source_chat_id":            source_message.chat.id,
        "source_chat_username":      _primary_username(source_message.chat),
        "source_message_id":         source_message.id,
        "message_id":                message.id,
        "file_id":                   file_id,
        "original_file_name":        original_file_name,
        "requested_output_filename": output_filename,
        "output_filename":           output_filename,
        "resolution":                "rename",
        "created_at":                created_at,
        "file_size":                 file_size,
        "send_type":                 settings_snapshot.get("send_type", "media"),
        "auto_detect_thumb":         bool(settings_snapshot.get("auto_detect_thumb", False)),
        "source_thumbnail_file_id":  source_thumbnail_file_id,
        "resolutions":               ["rename"],
        "jobs":                      [job],
        "total_jobs":                1,
        "current_job":               0,
        "current_stage":             "queued",
        "task_assets":               task_assets,
        "watermark":                 watermark_snapshot,
        "settings_snapshot":         settings_snapshot,
        "caption_template":          caption_template,
        "task_type":                 "rename",
        "batch_rename":              batch,
    }


def _snapshot_enqueue_assets(settings: dict, watermark: dict, temp_base: str) -> str:
    snapshot_dir = os.path.join(temp_base, f".enqueue_{uuid.uuid4().hex}")
    os.makedirs(snapshot_dir, exist_ok=True)

    source_thumb = str(settings.get("thumbnail_path") or "")
    if source_thumb and os.path.isfile(source_thumb):
        frozen_thumb = os.path.join(snapshot_dir, "thumbnail.jpg")
        shutil.copy2(source_thumb, frozen_thumb)
        settings["thumbnail_path"] = os.path.abspath(frozen_thumb)

    if isinstance(watermark, dict):
        source_font = str(watermark.get("font_path") or "")
        if source_font and os.path.isfile(source_font):
            ext = os.path.splitext(source_font)[1] or ".ttf"
            frozen_font = os.path.join(snapshot_dir, f"watermark_font{ext}")
            shutil.copy2(source_font, frozen_font)
            watermark["font_path"] = os.path.abspath(frozen_font)

    return snapshot_dir


def _freeze_task_assets(task_id: str, settings_snapshot: dict, watermark_snapshot: dict, temp_base: str) -> None:
    """Copy the thumbnail/watermark-font this task depends on into a folder owned
    by the task, and rewrite the paths in `settings_snapshot`/`watermark_snapshot`
    in place to point there.

    IMPORTANT: this must be called on the per-task settings/watermark snapshot
    *before* `_build_task`/`task_queue.create_task` — i.e. before the task exists
    in the queue or has been checkpointed — so the task is enqueued already in its
    final, immutable form instead of being queued first and mutated afterward.
    """
    task_folder = os.path.join(temp_base, task_id)

    source_thumb = str(settings_snapshot.get("thumbnail_path") or "")
    if source_thumb:
        if os.path.isfile(source_thumb):
            os.makedirs(task_folder, exist_ok=True)
            frozen_thumb = os.path.join(task_folder, f"thumbnail_{task_id}.jpg")
            try:
                shutil.copy2(source_thumb, frozen_thumb)
                settings_snapshot["thumbnail_path"] = os.path.abspath(frozen_thumb)
            except Exception:
                logger.exception("[rename] Could not freeze thumbnail for task %s", task_id)
                settings_snapshot["thumbnail_path"] = ""
        else:
            # Path no longer exists (e.g. cleared/replaced between settings capture
            # and freeze) - don't let a dangling path leak into the queued task.
            settings_snapshot["thumbnail_path"] = ""

    if isinstance(watermark_snapshot, dict):
        source_font = str(watermark_snapshot.get("font_path") or "")
        if source_font:
            if os.path.isfile(source_font):
                os.makedirs(task_folder, exist_ok=True)
                ext = os.path.splitext(source_font)[1] or ".ttf"
                frozen_font = os.path.join(task_folder, f"watermark_font_{task_id}{ext}")
                try:
                    shutil.copy2(source_font, frozen_font)
                    watermark_snapshot["font_path"] = os.path.abspath(frozen_font)
                except Exception:
                    logger.exception("[rename] Could not freeze watermark font for task %s", task_id)
                    watermark_snapshot["font_path"] = ""
            else:
                watermark_snapshot["font_path"] = ""


async def _process_single_rename(
    client: Client,
    message: Message,
    filename: str,
    task_queue,
    user_settings,
    temp_base: str,
):
    if not message.reply_to_message:
        await message.reply_text(
            "<b>▸ Usage</b>\n"
            "────────────────\n"
            'Reply to a video file.\n'
            '<blockquote><code>/rename2 "movie.mkv"</code></blockquote>',
            parse_mode=ParseMode.HTML,
        )
        return

    replied = message.reply_to_message

    if not _is_video_message(replied):
        await message.reply_text(
            f"<b>▸ Unsupported File</b>\n"
            f"────────────────\n"
            f"Only video files are supported.\n"
            f"Detected → <code>{_media_debug_label(replied)}</code>\n"
            f"Allowed → <code>{escape(', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS)))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not filename:
        _, filename, _ = _file_info(replied)

    if not _valid_extension(filename):
        await message.reply_text(
            f"<b>▸ Invalid Extension</b>\n"
            f"────────────────\n"
            f"Allowed → <code>{escape(', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS)))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    file_id, original_file_name, file_size = _file_info(replied)

    if not is_dc_allowed(file_id):
        await message.reply_text(
            "<b>▸ Blocked</b>\n"
            "────────────────\n"
            "<i>This file is blocked by the current DC filter, so it was not queued.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    user_id      = message.from_user.id
    settings_obj = user_settings(user_id)
    settings         = copy.deepcopy(settings_obj.get())
    watermark        = copy.deepcopy(settings_obj.get_watermark())
    caption_template = settings_obj.get_caption_template()

    created_at = datetime.utcnow().isoformat()

    # Reserve the id up front so assets can be frozen into a task-owned folder
    # *before* the task is built and queued (no await happens between the settings
    # snapshot above and this freeze, so there's no window for the live thumbnail/
    # font to change out from under us).
    task_id = uuid.uuid4().hex
    _freeze_task_assets(task_id, settings, watermark, temp_base)

    task_data = _build_task(
        task_id=task_id,
        message=message,
        source_message=replied,
        file_id=file_id,
        original_file_name=original_file_name,
        file_size=file_size,
        source_thumbnail_file_id=_source_thumbnail_file_id(replied),
        output_filename=filename,
        settings=settings,
        watermark=watermark,
        caption_template=caption_template,
        created_at=created_at,
        batch=False,
    )

    task_id  = task_queue.create_task(task_data)
    position = task_queue.get_queue_position(task_id)

    mode = "rename + metadata"

    await message.reply_text(
        f"<b>▸ Task Queued</b>\n"
        f"────────────────\n"
        f"┃ File : <code>{escape(filename)}</code>\n"
        f"┃ Position : <code>[{position}]</code>\n"
        f"┃ Task ID : <code>{escape(str(task_id))}</code>\n"
        f"┖ Mode : <code>{mode}</code>\n\n"
        f"<blockquote><u>Output will be delivered to your DM.</u> Please wait patiently.</blockquote>",
        parse_mode=ParseMode.HTML,
    )


async def _process_batch_rename(
    client: Client,
    message: Message,
    template: str,
    batch_count,
    task_queue,
    user_settings,
    temp_base: str,
):
    if not message.reply_to_message:
        await message.reply_text(
            "<b>▸ Usage</b>\n"
            "────────────────\n"
            "Reply to the <u>first</u> file.\n\n"
            "<blockquote>"
            "Media group : <code>/rename2 -b [E{episode}] Show Name.mkv</code>\n"
            "Sequential  : <code>/rename2 -b 6 [E{episode}] Show Name.mkv</code>"
            "</blockquote>",
            parse_mode=ParseMode.HTML,
        )
        return

    replied = message.reply_to_message

    ok, err = _validate_batch_template(template)
    if not ok:
        await message.reply_text(
            f"<b>▸ Error</b>\n"
            f"────────────────\n"
            f"<code>{err}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not _valid_extension(template):
        await message.reply_text(
            f"<b>▸ Invalid Extension</b>\n"
            f"────────────────\n"
            f"Invalid file extension in template.\n"
            f"Allowed → <code>{escape(', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS)))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    user_id      = message.from_user.id
    settings_obj = user_settings(user_id)
    settings         = copy.deepcopy(settings_obj.get())
    watermark        = copy.deepcopy(settings_obj.get_watermark())
    caption_template = settings_obj.get_caption_template()

    enqueue_snapshot_dir = _snapshot_enqueue_assets(settings, watermark, temp_base)
    try:
        episode_raw = str(settings.get("default_start_episode", "1"))
        ep_width    = max(len(episode_raw), 2)
        ep_int      = int(episode_raw)

        if batch_count is not None:
            status_msg  = await message.reply_text(
                f"<b>▸ Fetching</b>\n"
                f"────────────────\n"
                f"<i>Fetching {batch_count} messages…</i>",
                parse_mode=ParseMode.HTML,
            )
            raw_msgs    = await fetch_sequential_messages(
                client, message.chat.id, replied.id, batch_count
            )
        else:
            if not replied.media_group_id:
                await message.reply_text(
                    "<b>▸ Not An Album</b>\n"
                    "────────────────\n"
                    "The replied message is not part of a media group (album).\n"
                    "Send your files together as an album and reply to the first one,\n"
                    "or use <code>-b &lt;N&gt;</code> to grab N individual messages starting from the replied one.",
                    parse_mode=ParseMode.HTML,
                )
                return
            status_msg  = await message.reply_text(
                "<b>▸ Fetching</b>\n"
                "────────────────\n"
                "<i>Fetching media group…</i>",
                parse_mode=ParseMode.HTML,
            )
            raw_msgs    = await fetch_media_group(client, message.chat.id, replied)

        if not raw_msgs:
            await _safe_edit(
                status_msg,
                "<b>▸ Nothing Found</b>\n"
                "────────────────\n"
                + ("<i>Make sure you replied to the first file of the group.</i>" if batch_count is None
                   else f"<i>No video/document messages found in the next {batch_count} message IDs.</i>")
            )
            return

        valid_files: list[Message] = []
        skipped_unsupported = 0
        skipped_dc = 0
        for mg_msg in raw_msgs:
            if not _is_video_message(mg_msg):
                skipped_unsupported += 1
                continue

            file_id = _file_info(mg_msg)[0]
            if not is_dc_allowed(file_id):
                skipped_dc += 1
                continue

            valid_files.append(mg_msg)

        if not valid_files:
            header = "<b>▸ Nothing Queued</b>\n────────────────\n"
            if skipped_dc and not skipped_unsupported:
                text = (
                    header
                    + "<i>No files were queued — every video was blocked by the current DC filter.</i>"
                )
            elif skipped_dc:
                text = (
                    header
                    + f"No supported video files were queued.\n"
                    f"┠ Blocked by DC filter : <code>{skipped_dc}</code>\n"
                    f"┠ Unsupported/non-video : <code>{skipped_unsupported}</code>\n"
                    f"┖ Allowed : <code>{', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}</code>"
                )
            else:
                text = (
                    header
                    + f"No supported video files found.\n"
                    f"┖ Allowed : <code>{', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}</code>"
                )
            await _safe_edit(
                status_msg,
                text
            )
            return

        created_at = datetime.utcnow().isoformat()
        task_ids:  list[str] = []
        positions: list[int] = []
        episodes:  list[int] = []

        for index, mg_msg in enumerate(valid_files):
            ep_num = ep_int + index
            ep_str = str(ep_num).zfill(ep_width)
            episodes.append(ep_num)

            output_filename = _resolve_template(template, ep_str)
            file_id, original_file_name, file_size = _file_info(mg_msg)

            # `settings`/`watermark` here already point at the shared, pre-fetch
            # snapshot dir (see `_snapshot_enqueue_assets` above) rather than the
            # user's live, mutable files. Each task still gets its own deep copy
            # and its own frozen, task-owned asset files before it's ever built or
            # queued.
            task_id = uuid.uuid4().hex
            task_settings = copy.deepcopy(settings)
            task_watermark = copy.deepcopy(watermark)
            _freeze_task_assets(task_id, task_settings, task_watermark, temp_base)

            task_data = _build_task(
                task_id=task_id,
                message=message,
                source_message=mg_msg,
                file_id=file_id,
                original_file_name=original_file_name,
                file_size=file_size,
                source_thumbnail_file_id=_source_thumbnail_file_id(mg_msg),
                output_filename=output_filename,
                settings=task_settings,
                watermark=task_watermark,
                caption_template=caption_template,
                created_at=created_at,
                batch=True,
            )

            task_id  = task_queue.create_task(task_data)
            position = task_queue.get_queue_position(task_id)
            task_ids.append(task_id)
            positions.append(position)

        ep_start = str(episodes[0]).zfill(ep_width)
        ep_end   = str(episodes[-1]).zfill(ep_width)
        pos_min  = min(positions)
        pos_max  = max(positions)
        pos_text = f"[{pos_min}]" if pos_min == pos_max else f"[{pos_min} – {pos_max}]"

        mode = "rename + metadata"

        mode_label = f"sequential ({batch_count} msgs)" if batch_count else "media group"
        lines = [
        f"<b>▸ Batch Queued</b>\n────────────────",
        f"┃ Added : <code>{len(valid_files)}</code> task(s) {pos_text} <i>({mode_label})</i>",
        f"┠ Episodes : <code>{ep_start}</code> → <code>{ep_end}</code>",
        f"┖ Mode : <code>{mode}</code>",
    ]
        if skipped_unsupported:
            lines.append(f"<b>Skipped:</b> <code>{skipped_unsupported}</code> non-video file(s)")
        if skipped_dc:
            lines.append(f"<b>Blocked by DC filter:</b> <code>{skipped_dc}</code> file(s)")
        lines.append("\n<blockquote><u>Output will be delivered to your DM.</u> Please wait patiently.</blockquote>")

        await _safe_edit(status_msg, "\n".join(lines))
    finally:
        shutil.rmtree(enqueue_snapshot_dir, ignore_errors=True)


async def process_rename_command(
    client: Client,
    message: Message,
    task_queue,
    user_settings,
    temp_base: str,
    access_control,
):
    if not await _check_access(client, message, access_control):
        return

    is_batch, batch_count, filename, parse_error = _parse_rename_command(message)

    if parse_error:
        await message.reply_text(
            f"<b>▸ Error</b>\n"
            f"────────────────\n"
            f"<code>{parse_error}</code>\n\n"
            "<b>▸ Usage</b>\n"
            "<blockquote>"
            "Single     : <code>/rename2 movie.mkv</code>\n"
            "Batch group: <code>/rename2 -b [E{episode}] Show.mkv</code>\n"
            "Batch seq  : <code>/rename2 -b 6 [E{episode}] Show.mkv</code>"
            "</blockquote>",
            parse_mode=ParseMode.HTML,
        )
        return

    if is_batch:
        await _process_batch_rename(
            client, message, filename, batch_count, task_queue, user_settings, temp_base
        )
    else:
        await _process_single_rename(client, message, filename, task_queue, user_settings, temp_base)


def setup_rename_handler(app: Client, task_queue, user_settings, config, access_control):
    @app.on_message(command_filter(config, ["r", "rename"]) & group_scope_filter(config))
    async def rename_command(client: Client, message: Message):
        await process_rename_command(
            client, message, task_queue, user_settings, config.paths.tmp, access_control
        )
