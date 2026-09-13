from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.utils.commands import command_filter, command_text, chat_scope_filter


START_IMAGE_URL = "https://i.ibb.co/pvJNgMyw/b24974c41945bb7d21b50843400268c6.jpg"
DEVELOPER_CHANNEL_URL = "https://t.me/NyxyRen"
RENAME_GROUP_URL = "https://t.me/+Jb0U969PBsBmZjQx"


def _welcome_caption() -> str:
    return (
        "<b>▸ Welcome to Mirai Bot</b>\n"
        "────────────────\n"
        "I rename your video files right inside <u>Telegram</u> — fast, and "
        "without leaving the chat.\n\n"
        "<blockquote>Tap <b>Help</b> below to see everything I can do.</blockquote>"
    )


def _welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▸ Developer Channel", url=DEVELOPER_CHANNEL_URL)],
        [InlineKeyboardButton("▸ Rename Group", url=RENAME_GROUP_URL)],
        [InlineKeyboardButton("▸ Help", callback_data="mirai_help:0")],
    ])


def _help_pages(config, show_sudo: bool) -> list[tuple[str, str]]:
    cmd = lambda name: command_text(config, name)
    pages = [
        ("▸ Renaming", (
            "<b>▸ Renaming</b>\n"
            "────────────────\n"
            f"<code>{cmd('rename')} Movie.mkv</code>\n"
            "<i>Reply to a video with a new name.</i>\n\n"
            f"<code>{cmd('rename')} -b [S{{season}}-E{{episode}}] Show.mkv</code>\n"
            "<i>Reply to the first file of an album to batch-rename it.</i>\n\n"
            f"<code>{cmd('rename')} -b 6 [S{{season}}-E{{episode}}] Show.mkv</code>\n"
            "<i>Rename the next 6 messages starting from the replied file.</i>"
        )),
        ("▸ Settings", (
            "<b>▸ Settings</b>\n"
            "────────────────\n"
            f"<code>{cmd('es')}</code> → Open your settings menu.\n"
            f"<code>{cmd('ss')} 001</code> → Set the starting episode number.\n"
            f"<code>{cmd('st')}</code> → Reply to an image to save it as your thumbnail.\n\n"
            f"<blockquote><i>Delivery caption, thumbnail, metadata, and watermark are all "
            f"configurable from {cmd('es')}.</i></blockquote>"
        )),
        ("▸ Tasks", (
            "<b>▸ Tasks</b>\n"
            "────────────────\n"
            f"<code>{cmd('status')}</code> → Show the task queue and progress.\n"
            f"<code>{cmd('cancel')} &lt;task_id&gt;</code> → Cancel a queued or running task.\n"
            f"<code>{cmd('mi')}</code> → Reply to media to generate a MediaInfo report."
        )),
        ("▸ Premium", (
            "<b>▸ Premium</b>\n"
            "────────────────\n"
            f"<code>{cmd('plans')}</code> → See available Premium plans.\n"
            f"<code>{cmd('myplan')}</code> → Check your Premium status and expiry.\n\n"
            "<u>Most features need Premium</u> — ask an admin to upgrade you."
        )),
    ]
    if show_sudo:
        pages.append(("▸ Sudo", (
            "<b>▸ Sudo</b> <i>(owner-only)</i>\n"
            "────────────────\n"
            f"<code>{cmd('restart')}</code> → Restart the bot.\n"
            f"<code>{cmd('addpremium')} &lt;id&gt; [plan] [days]</code> → Grant Premium.\n"
            f"<code>{cmd('remove_premium')} &lt;id&gt;</code> → Revoke Premium.\n"
            f"<code>{cmd('premium_users')}</code> → List Premium users.\n"
            f"<code>{cmd('ban')} &lt;id&gt; [reason]</code> → Ban a user.\n"
            f"<code>{cmd('unban')} &lt;id&gt;</code> → Unban a user.\n"
            f"<code>{cmd('banned_users')}</code> → List banned users."
        )))
    return pages


def _help_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"mirai_help:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total}", callback_data="mirai_help:noop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"mirai_help:{page + 1}"))
    return InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("▸ Back to Start", callback_data="mirai_help:home")],
    ])


def build_help_text(config, *, show_owner_commands: bool = False) -> str:
    pages = _help_pages(config, show_owner_commands)
    return pages[0][1] if pages else ""


def setup_start_handler(app, config, access_control):
    @app.on_message(command_filter(config, ["start", "help"]) & chat_scope_filter(config))
    async def start_handler(client, message: Message):
        user = message.from_user
        if not user or await access_control.is_banned(user.id):
            return
        await message.reply_photo(
            START_IMAGE_URL,
            caption=_welcome_caption(),
            reply_markup=_welcome_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @app.on_callback_query(filters.regex(r"^mirai_help:"))
    async def help_page_callback(client, callback_query: CallbackQuery):
        user = callback_query.from_user
        if not user or await access_control.is_banned(user.id):
            await callback_query.answer()
            return

        raw = callback_query.data.split(":", 1)[1]

        if raw == "noop":
            await callback_query.answer()
            return

        if raw == "home":
            await callback_query.answer()
            try:
                await callback_query.message.edit_caption(
                    _welcome_caption(),
                    reply_markup=_welcome_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        try:
            page = int(raw)
        except ValueError:
            await callback_query.answer()
            return

        show_sudo = access_control.is_owner(user.id)
        pages = _help_pages(config, show_sudo)
        page = max(0, min(page, len(pages) - 1))

        await callback_query.answer()
        try:
            await callback_query.message.edit_caption(
                pages[page][1],
                reply_markup=_help_keyboard(page, len(pages)),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
