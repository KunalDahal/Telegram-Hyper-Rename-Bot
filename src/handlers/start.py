from pyrogram.enums import ParseMode
from pyrogram.types import Message

from src.utils.commands import command_filter, command_text, chat_scope_filter


def build_help_text(config, *, show_owner_commands: bool = False) -> str:
    cmd = lambda name: command_text(config, name)
    help_text = f"""<b>RenamerBot Help</b>

Reply to a video and use:
<code>{cmd('rename')} Movie.Name.2026.mkv</code>

For an album:
<code>{cmd('rename')} -b [S{{season}}-E{{episode}}] Show Name.mkv</code>

For the next N messages from a replied file:
<code>{cmd('rename')} -b 6 [S{{season}}-E{{episode}}] Show Name.mkv</code>

<b>Settings</b>

<code>{cmd('es')}</code> — Open settings.
<code>{cmd('ss')} 001</code> — Set the starting episode for batch names.
<code>{cmd('st')}</code> — Reply to an image to save a thumbnail.

<b>Tasks</b>

<code>{cmd('status')}</code> — Show queue and task IDs.
<code>{cmd('cancel')} &lt;task_id&gt;</code> — Cancel a queued or running task.

"""

    help_text += f"<code>{cmd('mi')}</code> — Reply to media for MediaInfo.\n"

    if not show_owner_commands:
        return help_text

    return help_text + f"""

<b>Bootstrap-owner commands</b>

<code>{cmd('add_admin')} &lt;user_id&gt;</code>: Add an admin by user ID.
<code>{cmd('remove_admin')} &lt;user_id&gt;</code>: Remove an admin by user ID.
<code>{cmd('list_admin')}</code>: list all admins. 
<code>{cmd('restart')}</code>: clear tasks and restart the bot.
"""


def setup_start_handler(app, config, access_control):
    @app.on_message(
        command_filter(config, ["start", "help"])
        & (chat_scope_filter(config))
    )
    async def start_handler(client, message: Message):
        user = message.from_user
        if not user or not await access_control.is_authorized(user.id):
            return
        show_owner_commands = bool(user and access_control.is_owner(user.id))
        await message.reply_text(
            build_help_text(config, show_owner_commands=show_owner_commands),
            parse_mode=ParseMode.HTML,
        )
