# Telegram Rename Bot

A Telegram video renamer built with Pyrogram and FFmpeg. Reply to a supported
video in an allowed group, choose an output filename, and receive the finished
file in your private chat with the bot.

The bot is designed for rename workflows: it preserves the source streams where
possible, while still supporting metadata, custom thumbnails, MediaInfo,
watermarks, and ordered batch processing.

## What it can do

- Rename individual video files and supported video documents.
- Rename a Telegram media album or a sequence of up to 150 messages.
- Build batch names with `{season}` and `{episode}`, including zero-padded
  episode numbers.
- Save per-user settings, thumbnails, and watermark fonts in MongoDB.
- Add title, author, artist, encoder, and other metadata without unnecessary
  video re-encoding.
- Generate a Telegraph-hosted MediaInfo page from a replied media file.
- Queue work globally, show live task status, and cancel queued or running jobs.
- Recover active task records after an unexpected restart.
- Restrict access to bootstrap owners and Premium users.
- Optionally use a Telegram Premium user session for downloads up to 4 GiB.

## Supported input formats

`.mp4`, `.mkv`, `.webm`, `.mov`, `.avi`, `.mpeg`, `.mpg`, `.wmv`, `.flv`, and
`.3gp`.

## Commands

Commands use the numeric `COMMAND_POSTFIX` configured for the bot. With the
default value `0`, use `/rename`; with `COMMAND_POSTFIX=2`, use `/rename2`.
Every command below follows this rule.

Most commands below require Premium access — see
[Access control](#access-control) below. `/plans` and `/myplan` are always
available so anyone can check what Premium unlocks and whether they have it.

| Command | Aliases | Use |
| --- | --- | --- |
| `/start` | `/help` | Open the bot help message. |
| `/rename <filename>` | `/r <filename>` | Reply to one video and queue a rename. If the filename is omitted, the original name is retained. |
| `/rename -b <template>` | `/r -b <template>` | Reply to the first item of a media album and batch-rename it. |
| `/rename -b <count> <template>` | `/r -b <count> <template>` | Rename the next 2–150 messages beginning at the replied file. |
| `/es` | `/us`, `/settings`, `/usersettings` | Open personal rename, metadata, thumbnail, send-mode, and watermark settings. |
| `/ss <episode>` | `/set_start_episode <episode>` | Set the starting episode number for batch names; `/ss 001` preserves the padding. |
| `/st` | `/setthumb` | Reply to an image to save it as your output thumbnail. |
| `/set_caption <caption>` | — | Set a custom delivery caption. Use `{filename}` as a placeholder for the output filename. Defaults to `<code>{filename}</code>`. |
| `/see_caption` | — | Show your saved custom caption (and a preview), or the default if you haven't set one. |
| `/del_caption` | — | Delete your custom caption and go back to the default. |
| `/status` | `/s` | Show task queue status and task IDs. |
| `/cancel <task_id>` | `/c <task_id>` | Cancel one queued or running task. |
| `/mi` | — | Reply to media to generate a MediaInfo page. |
| `/plans` | — | List available Premium plans. Open to everyone. |
| `/myplan` | — | Show your Premium status, plan, and expiry. Open to everyone. |
| `/restart` | — | Cancel tasks, remove task records and temporary files, then restart the bot. Owner-only. |

### Access control

Two groups can interact with the bot:

- **Bootstrap owners** (`OWNER_IDS`, comma-separated — more than one is
  supported) can use every Premium-protected feature plus every owner-only
  command below.
- **Premium users** (MongoDB-backed) can use the Premium-protected features
  (`/rename`, `/es`, `/ss`, `/st`, `/set_caption`, `/see_caption`,
  `/del_caption`, `/status`, `/cancel`, `/mi`), but not owner-only commands.
- **Everyone else** can use `/start`/`/help`, `/plans`, and `/myplan` only.

There is no separate admin tier — an owner grants elevated access to someone
else by making them Premium with `/addpremium`, not by adding them as an
admin.

### Owner-only commands

Only IDs listed in `OWNER_IDS` can use these commands.

| Command | Use |
| --- | --- |
| `/restart` | Cancel tasks, remove task records and temporary files, then restart the bot. |
| `/addpremium <user_id> [plan] [duration_days]` | Grant a user Premium access. Two plans are offered: `1 Month` (pass a `duration_days` of `30`) and `Lifetime` (omit `duration_days`, or pass `0`). `plan` defaults to `Standard` if omitted. |
| `/remove_premium <user_id>` | Revoke a user's Premium access. |
| `/premium_users` | List every Premium user, their plan, and their expiry. |
| `/ban <user_id> [reason]` | Ban a user from using the bot. Owners can't be banned. |
| `/unban <user_id>` | Unban a user. |
| `/banned_users` | List every currently banned user, with who banned them and why. |

A ban overrides Premium access once set, and works independently of it.

The "🗑 Cancel All" button on `/status` is also owner-only — Premium users
see the task list and pagination, but not that button.


## Rename examples

Reply to a video in an allowed group:

```text
/rename Movie.Name.2026.mkv
/rename "Movie Name 2026.mkv"
```

Reply to the first file in an album:

```text
/rename -b [S{season}-E{episode}] Show Name.mkv
```

Reply to the first of six sequential messages:

```text
/rename -b 6 [S{season}-E{episode}] Show Name.mkv
```

Only `{season}` and `{episode}` are supported in batch templates. Set the
starting episode with `/ss`; season and other preferences are available in the
settings panel.

> The bot must be started in private chat before a user queues work from a
> group, because finished files are delivered by DM.

## Requirements

- Python 3.10 or newer
- FFmpeg available on `PATH` (Docker installs it automatically)
- Telegram API ID and hash from [my.telegram.org](https://my.telegram.org)
- A bot token from [@BotFather](https://t.me/BotFather)
- A MongoDB database

## Configuration

Copy the example file and fill in the values:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

| Variable | Required | Description |
| --- | --- | --- |
| `API_ID` | Yes | Numeric Telegram API ID. |
| `API_HASH` | Yes | Telegram API hash. |
| `BOT_TOKEN` | Yes | Token issued by BotFather. |
| `ALLOWED_GROUP_IDS` | Yes | Comma-separated group IDs where rename commands may be used, for example `-1001234567890`. |
| `OWNER_IDS` | Yes | Comma-separated bootstrap owner Telegram user IDs. |
| `MONGO_URI` | Yes | MongoDB connection URI. |
| `MONGO_DB_NAME` | No | Database used by this bot; default: `renamer_bot`. Use a unique value per bot instance. |
| `SESSION_STRING` | No | Telegram Premium user-session string for downloads above 2 GiB. Leave empty for normal bot downloads. |
| `DUMP_CHAT_ID` | No | Optional dump chat used by the Premium user session for files above 2 GiB. Use a numeric chat ID, public username, or invite link. |
| `BOT_DUMP_CHAT_ID` | Yes | Bot-side dump chat for archived uploads. Use a numeric `-100...` ID or public `@username`; private invite URLs are not supported by bot sessions. |
| `WORKERS` | No | Single concurrency knob for the whole bot; default: `4`. Caps how many complete rename jobs run at once (premium + normal combined), and the same number caps simultaneous downloads and simultaneous uploads. |
| `COMMAND_POSTFIX` | No | Digits added to every command. Use `0` or leave empty for `/rename`; use `2` for `/rename2`. |
| `DC` | No | Comma-separated Telegram data centers (`1`–`5`) allowed for input files. Leave empty to allow all. |

Example:

```env
API_ID=123456
API_HASH=your_api_hash
BOT_TOKEN=123456:bot_token
ALLOWED_GROUP_IDS=-1001234567890
OWNER_IDS=123456789
MONGO_URI=mongodb+srv://user:password@cluster.example.mongodb.net/
MONGO_DB_NAME=renamer_bot_1
SESSION_STRING=
DUMP_CHAT_ID=
BOT_DUMP_CHAT_ID=-1001234567890
WORKERS=4
COMMAND_POSTFIX=0
DC=
```

Never commit `.env`, bot tokens, MongoDB credentials, or `SESSION_STRING`.
They are already excluded by `.gitignore`.

## Run locally

```bash
git clone https://github.com/KunalDahal/Telegram-Rename-Bot.git
cd Telegram-Rename-Bot
python -m venv .venv
```

Activate the virtual environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

Install dependencies and start the bot:

```bash
pip install -r requirements.txt
python run.py
```

## Docker

With `.env` in the repository root:

```bash
docker compose up -d --build
```

View logs with:

```bash
docker compose logs -f
```

The compose configuration persists `src/bin` for local logs and active work
files. MongoDB stores Premium/banned users, user settings, thumbnails,
watermark fonts, and task records.

## Heroku deployment

Open [deploy_heroku.ipynb](deploy_heroku.ipynb) in Google Colab or a compatible
Jupyter environment. It provides an interactive control center to:

1. Connect your Heroku account using an API key.
2. Create one or more container-stack apps.
3. Add the bot configuration as Heroku config vars through masked form inputs.
4. Deploy this repository and stream application logs.

Push your current code to GitHub before deploying—the notebook clones the
repository and branch you select (default: `master`).

### Multiple bots and dynos

Use one Heroku app and exactly one worker dyno per `BOT_TOKEN`. Telegram's
long-polling API does not permit two running workers for the same token.

For multiple bots, create one app per distinct token. They may share a MongoDB
cluster, but each must have a different `MONGO_DB_NAME`, for example
`renamer_bot_1` and `renamer_bot_2`. `COMMAND_POSTFIX` only changes command
names; it does not make duplicate workers for a token safe.

Heroku's filesystem is ephemeral. Downloaded work files are not durable across
a dyno replacement, but MongoDB-backed Premium/banned users, user settings,
thumbnails, watermark fonts, and task records persist.

## Premium download session

The standard bot client can download files up to 2 GiB. Set `SESSION_STRING` in
`.env` or as a Heroku config var to use a Telegram Premium user account for
downloads up to 4 GiB. The account must be Premium and able to access the
source chats. Leave `SESSION_STRING` empty to use normal bot downloads.

This setting affects downloads only. It does not give users access to the
Premium account or change where renamed files are sent.

## License

[MIT](LICENSE)
