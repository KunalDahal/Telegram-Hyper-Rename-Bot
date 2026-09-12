import os
import asyncio
import aiofiles
from pymediainfo import MediaInfo
from telegraph.aio import Telegraph
from telegraph.exceptions import RetryAfterError


class MediaInfoHelper:
    def __init__(self):
        self.telegraph    = Telegraph(domain="graph.org")
        self.access_token = None
        self.author_name  = "Encode Bot"
        self.author_url   = "https://t.me/your_bot_username"

    # ── Telegraph ─────────────────────────────────────────────────────────────

    async def create_account(self):
        if not self.access_token:
            await self.telegraph.create_account(
                short_name="encodebot",
                author_name=self.author_name,
                author_url=self.author_url,
            )
            self.access_token = self.telegraph.get_access_token()

    async def create_page(self, title: str, content: str) -> dict:
        try:
            return await self.telegraph.create_page(
                title=title,
                author_name=self.author_name,
                author_url=self.author_url,
                html_content=content,
            )
        except RetryAfterError as e:
            await asyncio.sleep(e.retry_after)
            return await self.create_page(title, content)

    # ── Partial download (Telegram files only) ────────────────────────────────

    async def download_partial(self, client, media, save_path: str,
                               max_bytes: int = 3 * 1024 * 1024):
        file_size = getattr(media, "file_size", None)

        if file_size and file_size <= max_bytes:
            await client.download_media(media, file_name=save_path)
            return

        received = 0
        async with aiofiles.open(save_path, "wb") as f:
            async for chunk in client.stream_media(media):
                await f.write(chunk)
                received += len(chunk)
                if received >= max_bytes:
                    break

    # ── pymediainfo parse (sync → run in executor) ───────────────────────────

    def _parse_sync(self, target: str) -> MediaInfo:
        return MediaInfo.parse(target)

    async def run_mediainfo(self, target: str) -> MediaInfo:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._parse_sync, target)

    # ── Build Telegraph HTML ──────────────────────────────────────────────────

    TRACK_META = {
        "General": ("🗒",  "General"),
        "Video":   ("🎞",  "Video"),
        "Audio":   ("🔊",  "Audio"),
        "Text":    ("🔠",  "Subtitle"),
        "Menu":    ("📑",  "Menu"),
        "Image":   ("🖼",  "Image"),
        "Other":   ("📄",  "Other"),
    }

    SKIP_FIELDS = {
        "track_type", "count", "stream_identifier", "streamorder",
        "other_format", "other_duration", "other_bit_rate",
        "other_width", "other_height", "other_frame_rate",
        "other_channel_s", "other_sampling_rate",
    }

    def _track_to_pre(self, track) -> str:
        lines = []
        for attr, value in track.__dict__.items():
            if attr.startswith("_"):
                continue
            if attr in self.SKIP_FIELDS:
                continue
            if value is None or value == "":
                continue

            val_str = str(value)
            label = (
                attr
                .replace("_", " ")
                .title()
                .replace("Id",  "ID")
                .replace("Fps", "FPS")
                .replace("Kb/S", "kb/s")
                .replace("Mb/S", "Mb/s")
                .replace("Yuv", "YUV")
                .replace("Uhd", "UHD")
                .replace("Hdr", "HDR")
                .replace("Url", "URL")
                .replace("Hevc", "HEVC")
                .replace("Avc",  "AVC")
                .replace("Aac",  "AAC")
                .replace("Mkv",  "MKV")
            )

            padded = f"{label:<40}: {val_str}"
            safe   = (
                padded
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            lines.append(safe)

        return "\n".join(lines)

    def build_html(self, media_info: MediaInfo, filename: str) -> str:
        html = f"<h4>📌 {filename}</h4>"
        type_counters: dict[str, int] = {}

        for track in media_info.tracks:
            ttype              = track.track_type
            emoji, base_label  = self.TRACK_META.get(ttype, ("📄", ttype))

            if ttype == "General":
                label = base_label
            else:
                count = type_counters.get(ttype, 0) + 1
                type_counters[ttype] = count
                label = base_label if count == 1 else f"{base_label} #{count}"

            body = self._track_to_pre(track)
            if not body.strip():
                continue

            html += f"<h4>{emoji} {label}</h4><pre>{body}</pre><br>"

        return html

    # ── Public entry point ────────────────────────────────────────────────────

    async def generate_mediainfo(self, target: str, filename: str):
        try:
            media_info = await self.run_mediainfo(target)

            if not media_info or not media_info.tracks:
                return None, "No media tracks found"

            html_content = self.build_html(media_info, filename)

            await self.create_account()
            page = await self.create_page(
                title=f"MediaInfo – {filename[:50]}",
                content=html_content,
            )

            return f"https://graph.org/{page['path']}", None

        except Exception as e:
            return None, str(e)