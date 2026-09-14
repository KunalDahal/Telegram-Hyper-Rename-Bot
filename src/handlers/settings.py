from collections import OrderedDict

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
import os
from html import escape
from pyrogram.enums import ParseMode
from src.utils.commands import command_filter, chat_scope_filter
from src.core.user_setting import DEFAULT_CAPTION_TEMPLATE

CAPTION_PLACEHOLDER = "{filename}"
_CAPTION_PREVIEW_NAME = "Sample.Movie.Name.S01E01.mkv"

_SETTINGS_OWNER_MAX = 2000
_settings_owner: "OrderedDict[tuple[int, int], int]" = OrderedDict()


def _set_settings_owner(chat_id: int, message_id: int, user_id: int) -> None:
    key = (chat_id, message_id)
    _settings_owner[key] = user_id
    _settings_owner.move_to_end(key)
    while len(_settings_owner) > _SETTINGS_OWNER_MAX:
        _settings_owner.popitem(last=False)  # evict oldest


def _get_settings_owner(chat_id: int, message_id: int) -> int | None:
    key = (chat_id, message_id)
    owner = _settings_owner.get(key)
    if owner is not None:
        _settings_owner.move_to_end(key)
    return owner

SETTINGS_IMAGE_URL = "https://i.ibb.co/RpZxdJwG/9a58b8252599bc747a0e0c7c6aa7c8b5.jpg"

METADATA_FIELDS = [
    ("title_all", "Title All", "Send the title to apply to general, video, audio, and subtitle metadata."),
    ("movie_name", "Movie Name", "Send the movie name to embed."),
    ("artist", "Artist", "Send the artist name to embed."),
    ("author", "Author", "Send the author name to embed."),
    ("encoder", "Encoder", "Send the encoder name to embed."),
]

METADATA_FIELD_BY_KEY = {key: (label, prompt) for key, label, prompt in METADATA_FIELDS}

def build_settings_text(
    name,
    username,
    user_id,
    settings,
    split_limit_gib: float = 1.95,
):
    has_thumb = bool(settings.get("thumbnail_path") and os.path.exists(settings.get("thumbnail_path", "")))
    send_type = "Media" if settings.get("send_type") == "media" else "Document"
    meta      = settings.get("metadata", {})
    meta_set  = any(meta.get(key) for key, _, _ in METADATA_FIELDS)

    ep = settings.get("default_start_episode", 1)

    cap_disabled = settings.get("caption_disabled", False)
    cap_custom   = settings.get("custom_caption", "")

    thumb_status = "<u>Set</u>"     if has_thumb    else "<i>Unset</i>"
    meta_status  = "<u>Set</u>"     if meta_set     else "<i>Unset</i>"
    if cap_disabled:
        cap_status = "<i>Disabled</i>"
    elif cap_custom:
        cap_status = "<u>Custom</u>"
    else:
        cap_status = "<i>Default</i>"

    display_name = escape(name) or "Unknown"
    display_user = f"@{escape(username)}" if username else "N/A"

    return (
        "<b>▸ Welcome to Settings</b>\n"
        "────────────────\n\n"
        f"<b>Split Size:</b>  <code>{split_limit_gib}GB</code>\n"
        f"<b>User:</b>  {display_name}  <code>{display_user}</code>  <code>{user_id}</code>\n\n"
        f"<b>Send Type:</b>  <code>{send_type}</code>\n"
        f"<b>Start Episode:</b>  <code>{ep}</code>\n"
        f"<b>Thumbnail:</b>  {thumb_status}\n"
        f"<b>Metadata:</b>  {meta_status}\n"
        f"<b>Caption:</b>  {cap_status}"
    )


def _split_limit_gib(premium_session_available: bool = False) -> float:
    return 3.95 if premium_session_available else 1.95

def build_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Send Type",     callback_data="set_send_type"),
         InlineKeyboardButton("Thumbnail",     callback_data="set_thumbnail")],
        [InlineKeyboardButton("Metadata",      callback_data="set_metadata"),
         InlineKeyboardButton("Start Episode", callback_data="set_start_episode")],
        [InlineKeyboardButton("Caption",       callback_data="set_caption")],
        [InlineKeyboardButton("Reset All",     callback_data="reset_settings"),
         InlineKeyboardButton("Close",         callback_data="close_menu")],
    ])

def build_metadata_text(meta: dict) -> str:
    sep = "─" * 19
    lines = "\n".join(
        f"{label} : <code>{meta.get(key) or '-'}</code>"
        for key, label, _ in METADATA_FIELDS
    )
    return (
        "<i>Set container and stream tags embedded directly into the output file.</i>\n"
        f"{sep}\n"
        f"{lines}\n"
        f"{sep}"
    )

def build_metadata_keyboard() -> InlineKeyboardMarkup:
    rows = []
    current = []
    for key, label, _ in METADATA_FIELDS:
        current.append(InlineKeyboardButton(label, callback_data=f"meta_set_{key}"))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([
        InlineKeyboardButton("Set All Metadata", callback_data="meta_set_all"),
    ])
    rows.append([
        InlineKeyboardButton("Clear All", callback_data="meta_clear"),
        InlineKeyboardButton("Back",      callback_data="back_to_menu"),
    ])
    return InlineKeyboardMarkup(rows)

def build_cancel_keyboard(label: str = "Cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="cancel_input")]
    ])

def build_thumbnail_text(settings: dict, subtitle: str = "") -> str:
    thumb = settings.get("thumbnail_path", "")
    has_thumb   = bool(thumb and os.path.exists(thumb))
    auto_detect = bool(settings.get("auto_detect_thumb", False))
    priority = (
        "Source file thumbnail first, then your saved thumbnail."
        if auto_detect else
        "Only your saved thumbnail is used."
    )
    extra = f"\n<i>{subtitle}</i>" if subtitle else ""
    return (
        f"<b>Thumbnail</b>{extra}\n\n"
        f"Saved Thumbnail : {'<code>Set</code>' if has_thumb else '<code>Not set</code>'}\n"
        f"Auto Detect     : <code>{'On' if auto_detect else 'Off'}</code>\n\n"
        f"<i>{priority}</i>\n\n"
        "<blockquote><i>If nothing is available after the selected priority, no thumbnail is applied.</i></blockquote>"
    )

def build_thumbnail_keyboard(settings: dict) -> InlineKeyboardMarkup:
    auto_detect  = bool(settings.get("auto_detect_thumb", False))
    toggle_label = "Auto Detect: On" if auto_detect else "Auto Detect: Off"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Upload / Replace",    callback_data="thumb_upload")],
        [InlineKeyboardButton(toggle_label,          callback_data="thumb_toggle_auto")],
        [InlineKeyboardButton("Remove Saved Thumb",  callback_data="thumb_remove")],
        [InlineKeyboardButton("Back",                callback_data="back_to_menu")],
    ])

def build_caption_text(settings: dict, subtitle: str = "") -> str:
    custom   = settings.get("custom_caption", "")
    disabled = settings.get("caption_disabled", False)
    extra    = f"\n<i>{subtitle}</i>" if subtitle else ""

    if disabled:
        current_line = "<i>Disabled — files are sent with no caption at all.</i>"
    elif custom:
        current_line = f"<code>{escape(custom)}</code>"
    else:
        current_line = f"<code>{escape(DEFAULT_CAPTION_TEMPLATE)}</code>  <i>(default)</i>"

    return (
        f"<b>Caption</b>{extra}\n\n"
        f"Current:\n{current_line}\n\n"
        f"Use <code>{escape(CAPTION_PLACEHOLDER)}</code> where the output filename should appear.\n\n"
        "<blockquote><i>This is the caption sent with files delivered to you — it has no "
        "effect on the dump-chat caption, which is fixed and applied separately.</i></blockquote>"
    )

def build_caption_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Caption",        callback_data="cap_view")],
        [InlineKeyboardButton("Edit Caption",   callback_data="cap_edit"),
         InlineKeyboardButton("Delete Caption", callback_data="cap_delete")],
        [InlineKeyboardButton("Back",           callback_data="back_to_menu")],
    ])

def build_start_episode_text(settings: dict, subtitle: str = "") -> str:
    current = settings.get("default_start_episode", 1)
    extra   = f"\n<i>{subtitle}</i>" if subtitle else ""
    return (
        f"<b>Start Episode</b>{extra}\n\n"
        f"Current: <code>{current}</code>\n\n"
        "This value is used for <code>{episode}</code> in batch rename templates.\n"
        "<blockquote><i>Send a new episode number to update it.</i></blockquote>"
    )

def build_start_episode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Set Start Episode", callback_data="start_episode_set")],
        [InlineKeyboardButton("Back",              callback_data="back_to_menu")],
    ])

def get_thumbnail_path(settings, config=None):
    thumb = settings.get("thumbnail_path", "")
    if thumb and os.path.exists(thumb):
        return thumb
    return None

def setup_settings_handlers(
    app: Client,
    user_settings,
    config,
    access_control,
    premium_session_checker=None,
):

    @app.on_message(
        command_filter(config, ["es", "us", "settings", "usersettings"])
        & (chat_scope_filter(config))
    )
    async def us_command(client: Client, message: Message):
        user_id  = message.from_user.id
        is_group = not message.chat.id == user_id
        chat_id  = message.chat.id

        if not await access_control.can_use_premium_features(user_id):
            return

        if not is_group:
            try:
                await client.get_chat(user_id)
            except Exception:
                bot_username = (await client.get_me()).username
                await message.reply_text(
                    f"Please start the bot in DM first.\n"
                    f"@{bot_username} — press <b>Start</b>, then try again.",
                    parse_mode=ParseMode.HTML,
                )
                return

        user     = message.from_user
        name     = f"{user.first_name or ''} {user.last_name or ''}".strip()
        username = user.username or ""
        settings = user_settings(user_id).get()

        text = build_settings_text(
            name,
            username,
            user_id,
            settings,
            split_limit_gib=_split_limit_gib(bool(premium_session_checker and premium_session_checker())),
        )
        keyboard        = build_main_keyboard()
        photo_source    = SETTINGS_IMAGE_URL

        try:
            sent = await client.send_photo(
                chat_id=chat_id,
                photo=photo_source,
                caption=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            _set_settings_owner(chat_id, sent.id, user_id)
        except Exception:
            await message.reply_text(
                "<b>▸ Error</b>\n"
                "────────────────\n"
                "<i>Failed to send settings. Please try again.</i>",
                parse_mode=ParseMode.HTML,
            )

    @app.on_callback_query(filters.regex(
        r"^(set_|sendtype_|meta_|reset_|back_to|close_|cancel_input|settings_|thumb_|start_episode_|cap_)"
    ))
    async def handle_settings_callbacks(client: Client, callback_query: CallbackQuery):
        user    = callback_query.from_user
        user_id = user.id
        data    = callback_query.data
        message = callback_query.message
        owner   = _get_settings_owner(message.chat.id, message.id)
        if owner is not None and owner != user_id:
            await callback_query.answer("This is not your settings menu.", show_alert=True)
            return
        if not await access_control.can_use_premium_features(user_id):
            await callback_query.answer("This is not your settings menu.", show_alert=True)
            return

        if data == "settings_noop":
            await callback_query.answer()
            return

        elif data == "set_send_type":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Media",    callback_data="sendtype_media"),
                 InlineKeyboardButton("Document", callback_data="sendtype_document")],
                [InlineKeyboardButton("Back", callback_data="back_to_menu")]
            ])
            await message.edit_text(
                "<b>Send Type</b>\n\n"
                "<b>Media</b> — sent as streamable video\n"
                "<b>Document</b> — sent as a raw file",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )

        elif data.startswith("sendtype_"):
            send_type = data.replace("sendtype_", "")
            user_settings(user_id).update("send_type", send_type)
            await callback_query.answer(f"Send type → {send_type.capitalize()}")
            await update_main_menu(client, message, user_id, config)
            return

        elif data == "set_metadata":
            meta = user_settings(user_id).get().get("metadata", {})
            await message.edit_text(
                build_metadata_text(meta),
                reply_markup=build_metadata_keyboard(),
                parse_mode=ParseMode.HTML
            )

        elif data == "meta_set_all":
            sent_message = await message.edit_text(
                "<b>Set All Metadata</b>\n\n"
                "Send one metadata value.\n\n"
                "<i>The same value will be applied to:</i>\n"
                "• <b>Title All</b>\n"
                "• <b>Movie Name</b>\n"
                "• <b>Artist</b>\n"
                "• <b>Author</b>\n"
                "• <b>Encoder</b>\n\n"
                "<i>Example: My Movie</i>",
                reply_markup=build_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            user_settings(user_id).temp_state[user_id] = {
                "chat_id": message.chat.id,
                "settings_message_id": message.id,
                "state": "waiting_meta_all",
                "prompt_message_id": sent_message.id,
                "back_to": "metadata",
            }
            await callback_query.answer()
            return

        elif data.startswith("meta_set_") or data in ("meta_title", "meta_author", "meta_encoder"):
            legacy_fields = {
                "meta_title":   "title_all",
                "meta_author":  "author",
                "meta_encoder": "encoder",
            }
            field = legacy_fields.get(data, data.replace("meta_set_", "", 1))
            if field not in METADATA_FIELD_BY_KEY:
                await callback_query.answer("Unknown metadata field", show_alert=True)
                return
            label, prompt = METADATA_FIELD_BY_KEY[field]
            sent_message = await message.edit_text(
                f"<b>Set {label}</b>\n\n{prompt}",
                reply_markup=build_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            user_settings(user_id).temp_state[user_id] = {
                "chat_id":            message.chat.id,
                "settings_message_id": message.id,
                "state":              f"waiting_meta_{field}",
                "prompt_message_id":  sent_message.id,
                "back_to":            "metadata",
            }

        elif data == "meta_clear":
            user_settings(user_id).update_metadata(
                **{key: "" for key, _, _ in METADATA_FIELDS}
            )
            await callback_query.answer("Metadata cleared")
            await update_main_menu(client, message, user_id, config)
            return

        elif data == "set_thumbnail":
            await update_thumbnail_menu(client, message, user_id)
            await callback_query.answer()
            return

        elif data == "thumb_upload":
            sent_message = await message.edit_text(
                "<b>Set Thumbnail</b>\n\nSend an image to use as the saved thumbnail.",
                reply_markup=build_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            user_settings(user_id).temp_state[user_id] = {
                "chat_id":            message.chat.id,
                "settings_message_id": message.id,
                "state":              "waiting_thumbnail",
                "prompt_message_id":  sent_message.id,
                "back_to":            "thumbnail",
            }
            await callback_query.answer()
            return

        elif data == "thumb_toggle_auto":
            us      = user_settings(user_id)
            current = bool(us.get().get("auto_detect_thumb", False))
            us.update("auto_detect_thumb", not current)
            await callback_query.answer(
                "Auto detect thumbnail enabled" if not current else "Auto detect thumbnail disabled"
            )
            await update_thumbnail_menu(client, message, user_id)
            return

        elif data == "thumb_remove":
            us = user_settings(user_id)
            us.clear_thumbnail()
            await callback_query.answer("Saved thumbnail removed")
            await update_thumbnail_menu(client, message, user_id)
            return

        elif data == "set_caption":
            settings = user_settings(user_id).get()
            await message.edit_text(
                build_caption_text(settings),
                reply_markup=build_caption_keyboard(),
                parse_mode=ParseMode.HTML
            )
            await callback_query.answer()
            return

        elif data == "cap_view":
            us = user_settings(user_id)
            if us.is_caption_disabled():
                popup = "No caption — files are sent without one."
            else:
                popup = us.get_caption() or DEFAULT_CAPTION_TEMPLATE
                if len(popup) > 200:
                    popup = popup[:197] + "..."
            await callback_query.answer(popup, show_alert=True)
            return

        elif data == "cap_edit":
            sent_message = await message.edit_text(
                "<b>Edit Caption</b>\n\n"
                "Send the new caption as HTML text — this is what gets sent "
                "with every file delivered to you.\n"
                f"Use <code>{escape(CAPTION_PLACEHOLDER)}</code> where the output filename "
                "should appear.\n\n"
                "Example:\n"
                f"<code>&lt;b&gt;{escape(CAPTION_PLACEHOLDER)}&lt;/b&gt;\n\n"
                "Uploaded by @MyChannel</code>\n\n"
                f"Default caption: <code>{escape(DEFAULT_CAPTION_TEMPLATE)}</code>",
                reply_markup=build_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            user_settings(user_id).temp_state[user_id] = {
                "chat_id":            message.chat.id,
                "settings_message_id": message.id,
                "state":              "waiting_caption",
                "prompt_message_id":  sent_message.id,
                "back_to":            "caption",
            }
            await callback_query.answer()
            return

        elif data == "cap_delete":
            us = user_settings(user_id)
            us.disable_caption()
            await callback_query.answer("Caption deleted — files will be sent without a caption")
            settings = us.get()
            await message.edit_text(
                build_caption_text(settings, "Caption deleted — files are sent with no caption"),
                reply_markup=build_caption_keyboard(),
                parse_mode=ParseMode.HTML
            )
            return

        elif data == "set_start_episode":
            settings = user_settings(user_id).get()
            await message.edit_text(
                build_start_episode_text(settings),
                reply_markup=build_start_episode_keyboard(),
                parse_mode=ParseMode.HTML
            )
            await callback_query.answer()
            return

        elif data == "start_episode_set":
            us      = user_settings(user_id)
            current = us.get().get("default_start_episode", 1)
            sent_msg = await message.edit_text(
                "<b>Set Start Episode</b>\n\n"
                f"Current: <code>{current}</code>\n\n"
                "Send the start episode number.\n"
                "<i>This value is used for <code>{episode}</code> in batch rename templates.</i>",
                reply_markup=build_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            us.temp_state[user_id] = {
                "chat_id":            message.chat.id,
                "settings_message_id": message.id,
                "state":              "waiting_start_episode",
                "prompt_message_id":  sent_msg.id,
                "back_to":            "start_episode",
            }
            await callback_query.answer()
            return

        elif data == "cancel_input":
            us         = user_settings(user_id)
            state_data = us.temp_state.get(user_id, {})
            back_to    = state_data.get("back_to", "main") if isinstance(state_data, dict) else "main"
            us.temp_state.pop(user_id, None)
            await callback_query.answer("Cancelled")

            if back_to == "metadata":
                meta = user_settings(user_id).get().get("metadata", {})
                await message.edit_text(
                    build_metadata_text(meta),
                    reply_markup=build_metadata_keyboard(),
                    parse_mode=ParseMode.HTML
                )
            elif back_to == "start_episode":
                settings = us.get()
                await message.edit_text(
                    build_start_episode_text(settings),
                    reply_markup=build_start_episode_keyboard(),
                    parse_mode=ParseMode.HTML
                )
            elif back_to == "thumbnail":
                await update_thumbnail_menu(client, message, user_id)
            elif back_to == "caption":
                settings = us.get()
                await message.edit_text(
                    build_caption_text(settings),
                    reply_markup=build_caption_keyboard(),
                    parse_mode=ParseMode.HTML
                )
            else:
                await update_main_menu(client, message, user_id, config)
            return

        elif data == "reset_settings":
            user_settings(user_id).reset()
            await callback_query.answer("Settings reset to defaults")
            await update_main_menu(client, message, user_id, config)
            return

        elif data == "back_to_menu":
            await update_main_menu(client, message, user_id, config)
            return

        elif data == "close_menu":
            await message.delete()
            await callback_query.answer("Menu closed")
            return

        await callback_query.answer()

    async def update_thumbnail_menu(client, message, user_id, subtitle=""):
        # `message` can be the result of a get_messages() re-fetch of a
        # message that's since been deleted - Pyrogram/wzgram represents
        # that as an "empty" Message (chat=None) rather than returning
        # None, so guard explicitly instead of touching message.chat.id.
        if message is None or getattr(message, "empty", False) or message.chat is None:
            return None

        settings       = user_settings(user_id).get()
        text           = build_thumbnail_text(settings, subtitle)
        keyboard       = build_thumbnail_keyboard(settings)
        thumbnail_path = get_thumbnail_path(settings, config)
        photo_source   = thumbnail_path or SETTINGS_IMAGE_URL
        chat_id        = message.chat.id

        try:
            if message.photo:
                await message.edit_media(
                    media=InputMediaPhoto(media=photo_source, caption=text, parse_mode=ParseMode.HTML),
                    reply_markup=keyboard
                )
                return message
            else:
                await message.delete()
                sent = await client.send_photo(
                    chat_id=chat_id, photo=photo_source, caption=text,
                    reply_markup=keyboard, parse_mode=ParseMode.HTML
                )
                _set_settings_owner(chat_id, sent.id, user_id)
                return sent
        except Exception:
            try:
                sent = await client.send_photo(
                    chat_id=chat_id, photo=photo_source, caption=text,
                    reply_markup=keyboard, parse_mode=ParseMode.HTML
                )
                _set_settings_owner(chat_id, sent.id, user_id)
                return sent
            except Exception:
                return None

    async def update_main_menu(client, message, user_id, config):
        user     = await client.get_users(user_id)
        name     = f"{user.first_name or ''} {user.last_name or ''}".strip()
        username = user.username or ""
        settings = user_settings(user_id).get()
        chat_id  = message.chat.id

        text = build_settings_text(
            name,
            username,
            user_id,
            settings,
            split_limit_gib=_split_limit_gib(bool(premium_session_checker and premium_session_checker())),
        )
        keyboard        = build_main_keyboard()
        photo_source    = SETTINGS_IMAGE_URL

        try:
            if message.photo:
                await message.edit_media(
                    media=InputMediaPhoto(media=photo_source, caption=text, parse_mode=ParseMode.HTML),
                    reply_markup=keyboard
                )
            else:
                await message.delete()
                sent = await client.send_photo(
                    chat_id=chat_id,
                    photo=photo_source,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                _set_settings_owner(chat_id, sent.id, user_id)
        except Exception:
            try:
                sent = await client.send_photo(
                    chat_id=chat_id, photo=photo_source, caption=text,
                    reply_markup=keyboard, parse_mode=ParseMode.HTML
                )
                _set_settings_owner(chat_id, sent.id, user_id)
            except Exception:
                pass

    _not_a_command = filters.create(lambda _, __, m: not (m.text or "").startswith("/"))

    @app.on_message(
        filters.text & _not_a_command & (chat_scope_filter(config))
    )
    async def handle_text_input(client: Client, message: Message):
        user_id = message.from_user.id

        us = user_settings(user_id)
        if user_id not in us.temp_state:
            return

        state_data        = us.temp_state[user_id]
        state             = state_data["state"] if isinstance(state_data, dict) else state_data
        prompt_message_id = state_data.get("prompt_message_id") if isinstance(state_data, dict) else None
        chat_id           = state_data.get("chat_id", user_id) if isinstance(state_data, dict) else user_id
        settings_message_id = state_data.get("settings_message_id") if isinstance(state_data, dict) else None

        if message.chat.id != chat_id:
            return

        async def _cleanup():
            ids = [i for i in [prompt_message_id, message.id] if i]
            if ids:
                try:
                    await client.delete_messages(chat_id=chat_id, message_ids=ids)
                except Exception:
                    pass

        async def _edit_settings(text, keyboard):
            if settings_message_id:
                try:
                    await client.edit_message_text(
                        chat_id=chat_id,
                        message_id=settings_message_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                    return
                except Exception:
                    pass
            sent = await client.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            _set_settings_owner(chat_id, sent.id, user_id)

        if state == "waiting_meta_all":
            value = message.text.strip()
            if not value:
                await message.reply_text(
                    "<b>▸ Empty Value</b>\n"
                    "────────────────\n"
                    "<i>Metadata value cannot be empty. Please send a value.</i>",
                    parse_mode=ParseMode.HTML
                )
                return

            us.update_metadata(**{
                key: value
                for key, _, _ in METADATA_FIELDS
            })
            del us.temp_state[user_id]
            await _cleanup()

            user_obj = message.from_user
            name     = f"{user_obj.first_name or ''} {user_obj.last_name or ''}".strip()
            username = user_obj.username or ""
            settings = us.get()
            text_out = build_settings_text(
                name, username, user_id, settings,
                split_limit_gib=_split_limit_gib(bool(premium_session_checker and premium_session_checker())),
            )
            await _edit_settings(text_out, build_main_keyboard())

        elif state.startswith("waiting_meta_"):
            field = state.replace("waiting_meta_", "", 1)
            if field == "title":
                field = "title_all"
            if field not in METADATA_FIELD_BY_KEY:
                await message.reply_text(
                    "<b>▸ Unknown Field</b>\n"
                    "────────────────\n"
                    "<i>That metadata field was not recognized.</i>",
                    parse_mode=ParseMode.HTML,
                )
                return
            us.update_metadata(**{field: message.text})
            del us.temp_state[user_id]
            await _cleanup()
            user_obj = message.from_user
            name     = f"{user_obj.first_name or ''} {user_obj.last_name or ''}".strip()
            username = user_obj.username or ""
            settings = us.get()
            text_out = build_settings_text(
                name, username, user_id, settings,
                split_limit_gib=_split_limit_gib(bool(premium_session_checker and premium_session_checker())),
            )
            await _edit_settings(text_out, build_main_keyboard())

        elif state == "waiting_caption":
            caption_text = (message.text or "").strip()
            if not caption_text:
                await message.reply_text(
                    "<b>▸ Empty Caption</b>\n"
                    "────────────────\n"
                    "Send some HTML text, or use "
                    "<b>Delete Caption</b> to send files with no caption at all.",
                    parse_mode=ParseMode.HTML
                )
                return

            preview = caption_text.replace(CAPTION_PLACEHOLDER, _CAPTION_PREVIEW_NAME)

            try:
                preview_msg = await client.send_message(
                    chat_id, f"<b>Preview</b>:\n{preview}", parse_mode=ParseMode.HTML
                )
            except Exception as e:
                await message.reply_text(
                    "<b>▸ Invalid HTML</b>\n"
                    "────────────────\n"
                    "That caption wasn't saved.\n"
                    f"<code>{escape(str(e))}</code>\n\n"
                    "<blockquote><i>Tip:</i> only Telegram's supported tags work here — "
                    "<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;u&gt;</code>, "
                    "<code>&lt;s&gt;</code>, <code>&lt;code&gt;</code>, <code>&lt;pre&gt;</code>, "
                    "<code>&lt;a href=...&gt;</code> — and every tag must be closed.</blockquote>",
                    parse_mode=ParseMode.HTML
                )
                return

            try:
                await client.delete_messages(chat_id=chat_id, message_ids=[preview_msg.id])
            except Exception:
                pass

            us.set_caption(caption_text)
            del us.temp_state[user_id]
            await _cleanup()
            settings = us.get()
            await _edit_settings(
                build_caption_text(settings, "Caption updated"),
                build_caption_keyboard(),
            )

        elif state == "waiting_start_episode":
            raw = message.text.strip()
            if not raw.isdigit() or int(raw) < 1:
                await message.reply_text(
                    "<b>▸ Invalid Value</b>\n"
                    "────────────────\n"
                    "Please send a valid episode number ≥ <code>1</code>.", parse_mode=ParseMode.HTML
                )
                return
            us.update("default_start_episode", raw)
            del us.temp_state[user_id]
            await _cleanup()
            settings = us.get()
            await _edit_settings(
                build_start_episode_text(settings, f"Start episode set to {raw}"),
                build_start_episode_keyboard(),
            )


    @app.on_message(filters.photo & (chat_scope_filter(config)))
    async def handle_thumbnail(client: Client, message: Message):
        user_id    = message.from_user.id
        us         = user_settings(user_id)
        if user_id not in us.temp_state:
            return

        state_data = us.temp_state[user_id]
        if not (isinstance(state_data, dict) and state_data.get("state") == "waiting_thumbnail"):
            return

        prompt_message_id   = state_data.get("prompt_message_id")
        chat_id             = state_data.get("chat_id", user_id)
        settings_message_id = state_data.get("settings_message_id")

        if message.chat.id != chat_id:
            return

        try:
            thumb_dir     = config.paths.thumbnails
            os.makedirs(thumb_dir, exist_ok=True)
            dest_path     = os.path.join(thumb_dir, f".upload_{user_id}_{os.urandom(8).hex()}.jpg")
            downloaded_path = await client.download_media(message, file_name=dest_path)

            if not downloaded_path or not os.path.exists(downloaded_path):
                await message.reply_text("<b>Failed to save thumbnail.</b> Please try again.", parse_mode=ParseMode.HTML)
                return

            us.set_thumbnail(os.path.abspath(downloaded_path))
            del us.temp_state[user_id]

            # `thumb_upload` builds the "Send an image..." prompt by editing
            # the settings menu message in place, so prompt_message_id and
            # settings_message_id are the SAME message. Only delete the
            # user's uploaded photo here - deleting the settings message
            # would leave settings_message_id pointing at nothing, and the
            # get_messages() call below would then hand back an empty
            # (chat=None) Message instead of raising.
            ids = [i for i in [prompt_message_id, message.id] if i and i != settings_message_id]
            if ids:
                try:
                    await client.delete_messages(chat_id=chat_id, message_ids=ids)
                except Exception:
                    pass

            settings_message = None
            if settings_message_id:
                try:
                    settings_message = await client.get_messages(chat_id, settings_message_id)
                    if settings_message is not None and getattr(settings_message, "empty", False):
                        settings_message = None
                except Exception:
                    settings_message = None

            if settings_message is not None:
                rendered = await update_thumbnail_menu(client, settings_message, user_id, "Thumbnail saved")
                if rendered is not None:
                    return

            result_text     = build_thumbnail_text(us.get(), "Thumbnail saved")
            result_keyboard = build_thumbnail_keyboard(us.get())
            thumbnail_path  = get_thumbnail_path(us.get(), config)
            sent = await client.send_photo(
                chat_id=chat_id, photo=thumbnail_path or SETTINGS_IMAGE_URL,
                caption=result_text, reply_markup=result_keyboard, parse_mode=ParseMode.HTML,
            )
            _set_settings_owner(chat_id, sent.id, user_id)

        except Exception as e:
            await message.reply_text(f"<b>Error saving thumbnail:</b> <code>{e}</code>", parse_mode=ParseMode.HTML)
