import asyncio
from datetime import datetime
from math import ceil, floor
from mimetypes import guess_extension
from os import path as ospath
from pathlib import Path
from re import sub
from sys import argv
from time import time

from aiofiles import open as aiopen
from aiofiles.os import makedirs, remove
from aioshutil import move

from pyrogram import raw, utils
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import PHOTO_TYPES, FileId, FileType, ThumbnailSource
from pyrogram.session import Auth, Session
from pyrogram.session.internals import MsgId


class HyperTGDownloader:
    def __init__(
        self,
        helper_bots: dict,
        helper_loads: dict,
        num_parts: int = None,
        chunk_size: int = 1024 * 1024,
        download_dir: str = "downloads/",
        logger=None,
    ):
        self.clients = helper_bots
        self.work_loads = helper_loads
        self.download_dir = download_dir
        self.num_parts = num_parts or max(8, len(self.clients))
        self.chunk_size = chunk_size
        self.logger = logger or print

        self.message = None
        self.dump_chat = None
        self.directory = None
        self.cache_file_ref = {}
        self.cache_last_access = {}
        self.cache_max_size = 100
        self._processed_bytes = 0
        self.file_size = 0
        self.file_name = ""
        self._cancel_event = asyncio.Event()
        self.session_pool = {}
        self._session_locks = {}

    async def start(self):
        asyncio.create_task(self._clean_cache())

    @staticmethod
    async def get_media_type(message):
        media_types = (
            "audio", "document", "photo", "sticker", "animation",
            "video", "voice", "video_note", "new_chat_photo"
        )
        for attr in media_types:
            if media := getattr(message, attr, None):
                return media
        raise ValueError("This message doesn't contain any downloadable media")

    def _update_cache(self, index, file_ref):
        self.cache_file_ref[index] = file_ref
        self.cache_last_access[index] = time()
        if len(self.cache_file_ref) > self.cache_max_size:
            oldest = sorted(self.cache_last_access.items(), key=lambda x: x[1])[0][0]
            del self.cache_file_ref[oldest]
            del self.cache_last_access[oldest]

    async def get_specific_file_ref(self, mid, client, max_retries=3):
        retries = 0
        last_error = None
        while retries < max_retries:
            try:
                media = await client.get_messages(self.dump_chat, mid)
                return FileId.decode(getattr(await self.get_media_type(media), "file_id", ""))
            except Exception as e:
                last_error = e
                retries += 1
                await asyncio.sleep(1 * retries)
        self.logger(
            f"Failed to get message {mid} from {self.dump_chat} with Client {getattr(client.me, 'username', client)}"
        )
        raise ValueError(
            f"Bot needs Admin access in Chat or message may be deleted. Error: {last_error}"
        )

    async def get_file_id(self, client, index) -> FileId:
        if index not in self.cache_file_ref:
            file_ref = await self.get_specific_file_ref(self.message.id, client)
            self._update_cache(index, file_ref)
        else:
            self.cache_last_access[index] = time()
        return self.cache_file_ref[index]

    async def _clean_cache(self):
        while True:
            await asyncio.sleep(15 * 60)
            current_time = time()
            expired_keys = [
                k for k, v in self.cache_last_access.items()
                if current_time - v > 45 * 60
            ]
            for key in expired_keys:
                if key in self.cache_file_ref:
                    del self.cache_file_ref[key]
                if key in self.cache_last_access:
                    del self.cache_last_access[key]

    async def generate_media_session(self, client, file_id, index, max_retries=3):
        session_key = (index, file_id.dc_id)
        if session_key in self.session_pool:
            return self.session_pool[session_key]
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            if session_key in self.session_pool:
                return self.session_pool[session_key]
            retries = 0
            last_error = None
            while retries < max_retries:
                try:
                    if hasattr(client, "get_session"):
                        media_session = await client.get_session(
                            dc_id=file_id.dc_id, is_media=True
                        )
                    elif file_id.dc_id != await client.storage.dc_id():
                        media_session = Session(
                            client,
                            file_id.dc_id,
                            await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(),
                            await client.storage.test_mode(),
                            is_media=True,
                        )
                        await media_session.start()
                        for _ in range(6):
                            exported_auth = await client.invoke(
                                raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                            )
                            try:
                                await media_session.invoke(
                                    raw.functions.auth.ImportAuthorization(
                                        id=exported_auth.id, bytes=exported_auth.bytes
                                    )
                                )
                                break
                            except AuthBytesInvalid:
                                await asyncio.sleep(1)
                        else:
                            await media_session.stop()
                            raise AuthBytesInvalid
                    else:
                        media_session = Session(
                            client,
                            file_id.dc_id,
                            await client.storage.auth_key(),
                            await client.storage.test_mode(),
                            is_media=True,
                        )
                        await media_session.start()
                    self.session_pool[session_key] = media_session
                    return media_session
                except Exception as e:
                    last_error = e
                    retries += 1
                    await asyncio.sleep(1)
            raise ValueError(f"Failed to create media session after {max_retries} attempts: {last_error}")

    @staticmethod
    async def get_location(file_id: FileId):
        file_type = file_id.file_type
        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                peer = (
                    raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                    if file_id.chat_access_hash == 0
                    else raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )
                )
            return raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            return raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )

    async def get_file(
        self,
        offset_bytes: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        max_retries=5,
    ):
        """Yield exactly the requested byte range using Telegram-safe blocks.

        Telegram's ``upload.getFile`` has strict request geometry: without the
        ``precise`` flag, offsets and limits must be aligned and each request
        must stay inside one 1 MiB file fragment. A previous implementation
        advanced the request offset by the *actual* response length when a short
        read occurred. That can produce an unaligned offset and immediately
        trigger ``400 LIMIT_INVALID``.

        We therefore keep request offsets on 1 MiB boundaries. A short response
        that does not satisfy the portion needed by the current range is retried
        from the same aligned offset instead of advancing into the middle of a
        Telegram block. Boundary bytes outside the current part are discarded.
        """
        index = min(self.work_loads, key=self.work_loads.get)
        client = self.clients[index]
        self.work_loads[index] += 1
        current_retry = 0
        try:
            aligned_start = offset_bytes
            target_end = (
                aligned_start + last_part_cut - 1
                if part_count == 1
                else aligned_start
                + (part_count - 1) * self.chunk_size
                + last_part_cut
                - 1
            )
            requested_start = aligned_start + first_part_cut
            block_offset = offset_bytes

            while block_offset <= target_end:
                if self._cancel_event.is_set():
                    raise asyncio.CancelledError("Download cancelled")

                try:
                    file_id = await self.get_file_id(client, index)
                    media_session, location = await asyncio.gather(
                        self.generate_media_session(client, file_id, index),
                        self.get_location(file_id),
                    )

                    # Always request a complete 1 MiB-aligned Telegram block.
                    # The server is allowed to return fewer bytes only at EOF;
                    # a short non-EOF response must be retried from the same
                    # offset. Never advance by a short response length.
                    requested = self.chunk_size
                    r = await asyncio.wait_for(
                        media_session.invoke(
                            raw.functions.upload.GetFile(
                                location=location,
                                offset=block_offset,
                                limit=requested,
                            ),
                        ),
                        timeout=30,
                    )

                    if not isinstance(r, raw.types.upload.File):
                        raise ValueError(f"Unexpected response: {r}")

                    chunk = r.bytes or b""
                    if len(chunk) > requested:
                        chunk = chunk[:requested]
                    if not chunk:
                        current_retry += 1
                        if current_retry >= max_retries:
                            raise ValueError(
                                f"Empty download block at offset {block_offset}; "
                                f"range ends at {target_end}"
                            )
                        await asyncio.sleep(min(2 ** current_retry, 8))
                        continue

                    block_end = block_offset + len(chunk) - 1
                    needed_end = min(target_end, block_offset + self.chunk_size - 1)
                    eof_reached = block_offset + len(chunk) >= self.file_size

                    # If this block is needed beyond what Telegram returned and
                    # we are not at EOF, retry exactly the same aligned request.
                    needed_bytes = max(0, needed_end - block_offset + 1)
                    if len(chunk) < needed_bytes and not eof_reached:
                        current_retry += 1
                        self.logger(
                            f"HyperDL short block at offset {block_offset}: "
                            f"got {len(chunk)} bytes, need {needed_bytes} "
                            f"(attempt {current_retry}/{max_retries}, client={index})"
                        )
                        if current_retry >= max_retries:
                            raise ValueError(
                                f"Short download block at offset {block_offset}: "
                                f"expected at least {needed_bytes} bytes, got {len(chunk)}"
                            )
                        await asyncio.sleep(min(2 ** current_retry, 8))
                        continue

                    current_retry = 0

                    # Keep only the intersection with this part's exact byte
                    # range. Bytes fetched only to satisfy Telegram's 1 MiB
                    # request geometry are intentionally discarded.
                    keep_start = max(block_offset, requested_start)
                    keep_end = min(block_end, target_end)
                    if keep_start <= keep_end:
                        relative_start = keep_start - block_offset
                        relative_end = keep_end - block_offset + 1
                        kept = chunk[relative_start:relative_end]
                        if kept:
                            yield kept
                            self._processed_bytes += len(kept)

                    # Advance only to the next Telegram-aligned block. This is
                    # the critical invariant that prevents LIMIT_INVALID.
                    block_offset += self.chunk_size

                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    continue
                except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                    current_retry += 1
                    if current_retry >= max_retries:
                        raise
                    await asyncio.sleep(min(2 ** current_retry, 8))
                    continue
                except Exception as e:
                    current_retry += 1
                    self.logger(
                        f"HyperDL chunk error at offset {block_offset} "
                        f"(attempt {current_retry}/{max_retries}, client={index}): "
                        f"{type(e).__name__}: {e}"
                    )
                    if current_retry >= max_retries:
                        raise
                    dc_id = None
                    try:
                        cached_ref = self.cache_file_ref.get(index)
                        dc_id = getattr(cached_ref, "dc_id", None)
                    except Exception:
                        pass
                    self.cache_file_ref.pop(index, None)
                    self.cache_last_access.pop(index, None)
                    if dc_id is not None:
                        media_key = (index, dc_id)
                        media_session = self.session_pool.pop(media_key, None)
                        self._session_locks.pop(media_key, None)
                        if media_session is not None:
                            try:
                                await media_session.stop()
                            except Exception:
                                pass
                    await asyncio.sleep(min(2 ** current_retry, 8))
                    continue

        finally:
            self.work_loads[index] -= 1

    async def progress_callback(self, progress, progress_args):
        if not progress:
            return
        while not self._cancel_event.is_set():
            try:
                if callable(progress):
                    await progress(
                        self._processed_bytes, self.file_size, *progress_args
                    )
                await asyncio.sleep(1)
            except (asyncio.CancelledError, Exception):
                break

    async def single_part(self, start, end, part_index, max_retries=5):
        until_bytes = min(end, self.file_size - 1)
        from_bytes = start
        offset = from_bytes - (from_bytes % self.chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut = until_bytes % self.chunk_size + 1
        part_count = (until_bytes // self.chunk_size) - (offset // self.chunk_size) + 1
        expected_size = until_bytes - from_bytes + 1
        part_file_path = ospath.join(
            self.directory, f"{self.file_name}.temp.{part_index:02d}"
        )

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # Always overwrite a failed partial part.  Never append to a
                # previous attempt: get_file() resumes from Telegram offsets
                # and this file must contain exactly this range once.
                async with aiopen(part_file_path, "wb") as f:
                    async for chunk in self.get_file(
                        offset, first_part_cut, last_part_cut, part_count,
                    ):
                        if self._cancel_event.is_set():
                            raise asyncio.CancelledError("Download cancelled")
                        await f.write(chunk)

                actual_size = ospath.getsize(part_file_path)
                if actual_size != expected_size:
                    raise ValueError(
                        f"Part {part_index} size mismatch: expected {expected_size} "
                        f"bytes, got {actual_size} bytes"
                    )
                return part_index, part_file_path

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                self.logger(
                    f"HyperDL part {part_index} failed "
                    f"(attempt {attempt}/{max_retries}, range={from_bytes}-{until_bytes}): "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    if ospath.exists(part_file_path):
                        await remove(part_file_path)
                except Exception:
                    pass
                if attempt < max_retries:
                    await asyncio.sleep(min(2 ** attempt, 10))

        raise RuntimeError(
            f"HyperDL part {part_index} failed after {max_retries} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

    async def handle_download(self, progress, progress_args):
        self._cancel_event.clear()
        await makedirs(self.directory, exist_ok=True)
        temp_file_path = (
            ospath.abspath(
                sub("\\\\", "/", ospath.join(self.directory, self.file_name))
            )
            + ".temp"
        )
        num_parts = min(self.num_parts, max(1, self.file_size // (10 * 1024 * 1024)))
        if self.file_size < 10 * 1024 * 1024:
            num_parts = 1
        # Integer division below almost never divides file_size evenly, so
        # num_parts * part_size is routinely a few bytes short of file_size
        # (e.g. 1379281175 // 8 == 172410146, and 8 * 172410146 ==
        # 1379281168 -- 7 bytes short). The last part's end MUST always be
        # pinned to file_size - 1 so that remainder always lands in the
        # final part instead of being silently dropped, which is what
        # caused "Combined download size mismatch" errors.
        part_size = self.file_size // num_parts if num_parts > 0 else self.file_size
        ranges = [
            (
                i * part_size,
                self.file_size - 1
                if i == num_parts - 1
                else (i + 1) * part_size - 1,
            )
            for i in range(num_parts)
        ]
        tasks = []
        prog_task = None
        try:
            for i, (start, end) in enumerate(ranges):
                tasks.append(asyncio.create_task(self.single_part(start, end, i)))
            if progress:
                prog_task = asyncio.create_task(self.progress_callback(progress, progress_args))
            results = await asyncio.gather(*tasks)
            if prog_task and not prog_task.done():
                prog_task.cancel()
                # Wait for the progress callback to stop before final assembly.
                await asyncio.gather(prog_task, return_exceptions=True)
            async with aiopen(temp_file_path, "wb") as temp_file:
                for _, part_file_path in sorted(results, key=lambda x: x[0]):
                    try:
                        async with aiopen(part_file_path, "rb") as part_file:
                            while True:
                                chunk = await part_file.read(8 * 1024 * 1024)
                                if not chunk:
                                    break
                                await temp_file.write(chunk)
                        await remove(part_file_path)
                    except Exception as e:
                        self.logger(
                            f"Error processing part file {part_file_path}: {e}"
                        )
                        raise
            file_path = ospath.splitext(temp_file_path)[0]
            combined_size = ospath.getsize(temp_file_path)
            if combined_size != self.file_size:
                raise ValueError(
                    f"Combined download size mismatch: expected {self.file_size} bytes, "
                    f"got {combined_size} bytes"
                )
            await move(temp_file_path, file_path)
            return file_path
        except FloodWait:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger(
                f"HyperDL Error ({type(e).__name__}): {e}"
            )
            raise RuntimeError(
                f"HyperTG download failed: {type(e).__name__}: {e}"
            ) from e
        finally:
            self._cancel_event.set()
            if prog_task and not prog_task.done():
                prog_task.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            for i in range(len(ranges)):
                part_path = ospath.join(
                    self.directory, f"{self.file_name}.temp.{i:02d}"
                )
                try:
                    if ospath.exists(part_path):
                        await remove(part_path)
                except Exception:
                    pass

    @staticmethod
    async def get_extension(file_type, mime_type):
        if file_type in PHOTO_TYPES:
            return ".jpg"
        if mime_type:
            extension = guess_extension(mime_type)
            if extension:
                return extension
        if file_type == FileType.VOICE:
            return ".ogg"
        elif file_type in (FileType.VIDEO, FileType.ANIMATION, FileType.VIDEO_NOTE):
            return ".mp4"
        elif file_type == FileType.DOCUMENT:
            return ".bin"
        elif file_type == FileType.STICKER:
            return ".webp"
        elif file_type == FileType.AUDIO:
            return ".mp3"
        else:
            return ".bin"

    async def download_media(
        self,
        message,
        file_name="downloads/",
        progress=None,
        progress_args=(),
        dump_chat=None,
    ):
        try:
            if dump_chat:
                bot_client = list(self.clients.values())[0]
                copied_message = await bot_client.copy_message(
                    chat_id=dump_chat,
                    from_chat_id=message.chat.id,
                    message_id=message.id,
                    disable_notification=True
                )
                self.message = copied_message
            else:
                self.message = message

            self.dump_chat = dump_chat or message.chat.id

            media = await self.get_media_type(self.message)
            file_id_str = media if isinstance(media, str) else media.file_id
            file_id_obj = FileId.decode(file_id_str)
            file_type = file_id_obj.file_type
            media_file_name = getattr(media, "file_name", "")
            self.file_size = getattr(media, "file_size", 0)
            mime_type = getattr(media, "mime_type", "image/jpeg")
            date = getattr(media, "date", None)
            self.directory, self.file_name = ospath.split(file_name)
            self.file_name = self.file_name or media_file_name or ""
            if not ospath.isabs(self.file_name):
                self.directory = Path(argv[0]).parent / (
                    self.directory or self.download_dir
                )
            if not self.file_name:
                extension = await self.get_extension(file_type, mime_type)
                self.file_name = f"{FileType(file_id_obj.file_type).name.lower()}_{(date or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S')}_{MsgId()}{extension}"
            return await self.handle_download(progress, progress_args)
        except Exception as e:
            self.logger(f"Download media error: {e}")
            raise
