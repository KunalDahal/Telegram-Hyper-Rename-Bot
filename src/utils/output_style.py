
import logging
from functools import wraps

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import (
    InlineKeyboardButton,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from src.utils.stylize import font, stylize_html

logger = logging.getLogger(__name__)

_PATCHED = False


def _wrap_outgoing(orig_func, *, param_name, position=None, default_parse_mode=True):

    @wraps(orig_func)
    async def wrapper(*args, **kwargs):
        if param_name in kwargs and isinstance(kwargs[param_name], str):
            kwargs[param_name] = stylize_html(kwargs[param_name])
        elif (
            position is not None
            and len(args) > position
            and isinstance(args[position], str)
        ):
            args = list(args)
            args[position] = stylize_html(args[position])
        if default_parse_mode:
            kwargs.setdefault("parse_mode", ParseMode.HTML)
        return await orig_func(*args, **kwargs)

    return wrapper


def _wrap_media_caption_init(orig_init):

    @wraps(orig_init)
    def wrapper(self, *args, **kwargs):
        if "caption" in kwargs and isinstance(kwargs["caption"], str):
            kwargs["caption"] = stylize_html(kwargs["caption"])
        kwargs.setdefault("parse_mode", ParseMode.HTML)
        return orig_init(self, *args, **kwargs)

    return wrapper


def _wrap_button_init(orig_init):

    @wraps(orig_init)
    def wrapper(self, text=None, *args, **kwargs):
        if isinstance(text, str):
            text = font(text)
        return orig_init(self, text, *args, **kwargs)

    return wrapper


def _wrap_callback_answer(orig_answer):

    @wraps(orig_answer)
    async def wrapper(self, text=None, *args, **kwargs):
        if isinstance(text, str):
            text = font(text)
        elif "text" in kwargs and isinstance(kwargs["text"], str):
            kwargs["text"] = font(kwargs["text"])
        return await orig_answer(self, text, *args, **kwargs)

    return wrapper


def apply_global_styling() -> None:
    global _PATCHED
    if _PATCHED:
        return

    Message.reply_text = _wrap_outgoing(Message.reply_text, param_name="text", position=1)
    Message.edit_text = _wrap_outgoing(Message.edit_text, param_name="text", position=1)
    if hasattr(Message, "edit_caption"):
        Message.edit_caption = _wrap_outgoing(
            Message.edit_caption, param_name="caption", position=1
        )
    for method_name in ("reply_photo", "reply_document", "reply_video", "reply_animation", "reply_audio"):
        if hasattr(Message, method_name):
            setattr(
                Message,
                method_name,
                _wrap_outgoing(getattr(Message, method_name), param_name="caption"),
            )

    Client.send_message = _wrap_outgoing(Client.send_message, param_name="text")
    Client.edit_message_text = _wrap_outgoing(Client.edit_message_text, param_name="text")
    if hasattr(Client, "edit_message_caption"):
        Client.edit_message_caption = _wrap_outgoing(
            Client.edit_message_caption, param_name="caption"
        )
    for method_name in ("send_photo", "send_document", "send_video", "send_animation", "send_audio"):
        if hasattr(Client, method_name):
            setattr(
                Client,
                method_name,
                _wrap_outgoing(getattr(Client, method_name), param_name="caption"),
            )

    InputMediaPhoto.__init__ = _wrap_media_caption_init(InputMediaPhoto.__init__)
    InputMediaDocument.__init__ = _wrap_media_caption_init(InputMediaDocument.__init__)
    InputMediaVideo.__init__ = _wrap_media_caption_init(InputMediaVideo.__init__)
    InlineKeyboardButton.__init__ = _wrap_button_init(InlineKeyboardButton.__init__)

    try:
        from pyrogram.types import CallbackQuery

        CallbackQuery.answer = _wrap_callback_answer(CallbackQuery.answer)
    except ImportError:  
        logger.warning("Could not patch CallbackQuery.answer for house-font styling.")

    _PATCHED = True
    logger.info(
        "[Style] House font + HTML parse mode now applied globally to all "
        "outgoing messages, captions, buttons, and alerts."
    )
