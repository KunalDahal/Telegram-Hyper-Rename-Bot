import os
import shutil
import sys
from typing import List
from dotenv import load_dotenv


def _find_binary(name: str, local_dir: str) -> str:
    found = shutil.which(name)
    if found:
        return found

    exe_suffix = ".exe" if sys.platform == "win32" else ""
    local_path = os.path.join(local_dir, f"{name}{exe_suffix}")

    if os.path.isfile(local_path):
        return local_path

    return name


class Paths:

    def __init__(self, base_dir: str):
        self.base = base_dir
        self.bin = os.path.join(base_dir, "bin")
        self.tmp = os.path.join(base_dir, "bin", "tmp")
        self.logs = os.path.join(base_dir, "bin", "logs")
        self.thumbnails = os.path.join(base_dir, "bin", "thumbnails")
        self.users = os.path.join(base_dir, "bin", "users")
        self.fonts = os.path.join(base_dir, "bin", "fonts")
        self.templates = os.path.join(base_dir, "templates")
        self.ffmpeg = _find_binary("ffmpeg", self.bin)

        self.start_image = "https://i.ibb.co/N6Fc2mbZ/start.png"
        self.help_banner = os.path.join(base_dir, "templates", "help.jpg")
        self.default_thumb = os.path.join(base_dir, "bin", "default.jpg")

    def makedirs(self):
        for d in (
            self.tmp,
            self.logs,
            self.thumbnails,
            self.users,
            self.fonts,
        ):
            os.makedirs(d, exist_ok=True)


class Config:
    _SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def __init__(self):
        load_dotenv()

        self.bot_token: str = os.getenv("BOT_TOKEN", "").strip()

        try:
            self.api_id: int = int(os.getenv("API_ID", "0"))
        except ValueError as exc:
            raise ValueError("API_ID must be an integer") from exc

        self.api_hash: str = os.getenv("API_HASH", "").strip()

        # Optional. Leave empty for a DM-only bot; commands always work in DM
        # regardless of this setting (see chat_scope_filter). Any IDs listed
        # here additionally allow the same commands inside those group chats.
        self.allowed_group_ids: List[int] = [
            gid for gid in self._parse_int_list(os.getenv("ALLOWED_GROUP_IDS", ""))
            if gid != 0
        ]
        self.owner_ids: List[int] = self._parse_int_list(
            os.getenv("OWNER_IDS", "")
        )

        self.sub_bot_tokens: List[str] = [
            token.strip()
            for token in os.getenv("SUB_BOT_TOKENS", "").split(",")
            if token.strip()
        ]

        self.mongo_uri: str = os.getenv("MONGO_URI", "").strip()

        self.mongo_db_name: str = os.getenv(
            "MONGO_DB_NAME",
            "renamer_bot",
        ).strip()

        self.bot_session_string: str = os.getenv(
            "BOT_SESSION_STRING",
            "",
        ).strip()

        self.session_string: str = os.getenv(
            "SESSION_STRING",
            "",
        ).strip()

        # Telegram targets stay as strings because:
        # - BOT_DUMP_CHAT_ID may be a numeric -100... ID, public @username,
        #   or a private invite URL for the bot-side dump.
        # - DUMP_CHAT_ID may be a private invite URL or public @username for
        #   the optional Premium SESSION_STRING.
        self.dump_chat_id: str | None = self._optional_chat_target_env(
            "DUMP_CHAT_ID"
        )
        self.bot_dump_chat_id: str | None = self._optional_chat_target_env(
            "BOT_DUMP_CHAT_ID"
        )

        # WORKERS is the single knob for all concurrency in the bot: it caps
        # how many complete rename jobs (download -> process -> upload) run
        # at once, and each stage (download, upload, watermark processing)
        # is allowed up to that same number in parallel. Premium and normal
        # tasks share this one pool -- e.g. WORKERS=4 means at most 4 jobs
        # total (premium + normal combined) run at a time, at most 4
        # downloads at a time, and at most 4 uploads at a time.
        self.workers = self._positive_int_env("WORKERS", default=4)

        # Kept as an alias so any code that still reads max_rename_at_once
        # keeps working.
        self.max_rename_at_once = self.workers

        # Pyrogram's bot Client separately takes max_concurrent_transmissions,
        # a SINGLE shared cap on that one Client's simultaneous network
        # transmissions for downloads AND uploads combined. It has to cover
        # a full download batch and a full upload batch running together
        # (WORKERS each), so it's sized at 2x WORKERS.
        self.bot_transmission_limit = self.workers * 2

        # UPLOAD_PART_WORKERS -> config.upload_part_workers -> the bot
        # Client's max_concurrent_transmissions (see __main__.py).
        self.upload_part_workers = self._positive_int_env("UPLOAD_PART_WORKERS", default=8)
        self.command_postfix = self._command_postfix_env()

        self.paths = Paths(self._SRC_DIR)

        self._validate()

    @staticmethod
    def _parse_int_list(raw: str) -> List[int]:
        if not raw:
            return []

        result: List[int] = []

        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue

            try:
                result.append(int(part))
            except ValueError:
                raise ValueError(
                    f"Expected comma-separated integer IDs, got {part!r}"
                )

        return result

    @staticmethod
    def _optional_chat_target_env(name: str) -> str | None:
        raw = os.getenv(name, "").strip()
        return raw or None

    @staticmethod
    def _positive_int_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default

        return value if value > 0 else default

    @staticmethod
    def _command_postfix_env() -> str:
        value = os.getenv("COMMAND_POSTFIX", "0").strip()

        if not value or value == "0":
            return ""

        if not value.isdigit():
            raise ValueError(
                "COMMAND_POSTFIX must be a non-negative integer"
            )

        return value

    def _validate(self):
        if not self.bot_token:
            raise ValueError("BOT_TOKEN is required")

        if not self.api_id:
            raise ValueError("API_ID is required")

        if not self.api_hash:
            raise ValueError("API_HASH is required")

        # ALLOWED_GROUP_IDS is optional: commands work in DM either way.

        if not self.owner_ids:
            raise ValueError("OWNER_IDS is required")

        if not self.mongo_uri:
            raise ValueError("MONGO_URI is required")

        if not self.mongo_db_name:
            raise ValueError("MONGO_DB_NAME cannot be empty")

        # Bot-side dump is independent from the optional Premium session.
        if self.bot_dump_chat_id is None:
            raise ValueError(
                "BOT_DUMP_CHAT_ID is required for the bot session. "
                "Use a numeric -100... ID, @username, or invite URL for "
                "the bot-side dump channel."
            )

        # DUMP_CHAT_ID is optional. Premium uploads use BOT_DUMP_CHAT_ID as
        # the canonical staging target so BOT_TOKEN can always copy the result
        # to the user's DM. DUMP_CHAT_ID is retained only for compatibility.
