import asyncio
import os
import shutil
import time

from src.utils.retry import call_with_flood_retry

MAX_NON_PREMIUM_BYTES = int(1.95 * 1024 ** 3)
MAX_PREMIUM_BYTES = int(3.95 * 1024 ** 3)

_SPEED_UPDATE_INTERVAL = 0.5
MAX_FLOOD_WAIT_RETRIES = 5


class Uploader:
    def __init__(self, client, task_data: dict, task_queue=None, tmp_dir: str = None, user_is_premium: bool = False):
        self.client = client
        self.task_data = task_data
        self.task_queue = task_queue
        self.user_is_premium = user_is_premium
        self._tmp_dir = tmp_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bin", "tmp",
        )

        self._last_time = None
        self._last_bytes = 0
        self._start_time = None
        self._grand_total_bytes = 0
        self._cached_speed = 0.0
        self._cached_eta = 0
        self._progress_lock = asyncio.Lock()

        # Bytes already accounted for from parts that finished before the
        # one currently being sent. Kurigram/Pyrogram's progress callback
        # reports (current, total) for the single file it is transmitting
        # right now, so this offset is what turns that into an overall
        # uploaded/total figure across every part of this task.
        self._part_base_bytes = 0

        self.upload_progress = {
            "total_size": 0,
            "uploaded": 0,
            "percentage": 0.0,
            "speed": 0.0,
            "eta": 0,
            "elapsed": 0,
            "status": "idle",
            "current_part": 1,
            "total_parts": 1,
        }

    def _max_part_size(self) -> int:
        return MAX_PREMIUM_BYTES if self.user_is_premium else MAX_NON_PREMIUM_BYTES

    def _dump_display_name(self) -> str:
        """Username (preferred, so the master bot / a human can tap through
        to the sender) or public first_name as a fallback when the user has
        no @username set."""
        username = str(self.task_data.get("username") or "").strip()
        if username:
            return f"@{username}"
        first_name = str(self.task_data.get("first_name") or "").strip()
        return first_name or "Unknown"

    def _dump_caption(self, task_id: str, user_id, part_name: str, part_idx: int, n_parts: int) -> str:
        """Caption for a message landing in the dump chat."""
        caption = (
            f"**{part_name}**\n\n"
            f"👤 **{self._dump_display_name()}**\n"
            f"🆔 User ID: `{user_id}`\n"
            f"🔖 Task ID: `{task_id}`"
        )
        if n_parts > 1:
            caption += f"\n📦 Part {part_idx} of {n_parts}"
        return caption

    async def upload(self):
        task_id = self.task_data["task_id"]
        user_id = self.task_data["user_id"]
        output_file_name = self.task_data["output_filename"]
        send_type = self.task_data.get("send_type", "media")
        upload_chat_id = self.task_data.get("upload_chat_id", user_id)

        task_folder = os.path.join(self._tmp_dir, task_id)
        explicit_file_path = self.task_data.get("upload_file_path")

        if explicit_file_path and os.path.exists(explicit_file_path):
            final_file_path = explicit_file_path
        else:
            media_files = sorted([
                f for f in os.listdir(task_folder)
                if f.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm"))
            ])
            if not media_files:
                raise Exception("No media file found in task folder")

            current_file = os.path.join(task_folder, media_files[0])
            final_file_path = os.path.join(task_folder, output_file_name)
            if current_file != final_file_path:
                shutil.move(current_file, final_file_path)

        if not os.path.exists(final_file_path):
            raise Exception("File not found after renaming")

        file_size = os.path.getsize(final_file_path)
        max_part_sz = self._max_part_size()
        thumb = self.task_data.get("thumbnail_path")

        if file_size > max_part_sz:
            if not self.user_is_premium:
                raise Exception(
                    f"Non-Premium upload file is {file_size / (1024**3):.2f} GiB; "
                    f"maximum per part is {max_part_sz / (1024**3):.2f} GiB."
                )

        # Telegram's file-part limit is separate from our application-level
        # split limit. For ordinary files we hand the whole file straight to
        # Kurigram/Pyrogram's native send_video/send_document, which uploads
        # it via save_file()'s own concurrent MTProto transmissions. Oversized
        # application files retain the old FFmpeg-independent split behavior.
        if file_size > max_part_sz:
            parts = await self._split_file(
                final_file_path, max_part_sz, output_file_name, task_folder
            )
        else:
            parts = [(final_file_path, output_file_name)]

        n_parts = len(parts)
        self._grand_total_bytes = sum(os.path.getsize(p) for p, _ in parts)
        self.upload_progress.update({
            "total_size": self._grand_total_bytes,
            "total_parts": n_parts,
            "status": "uploading",
        })
        self._start_time = time.time()
        self._last_time = None
        self._last_bytes = 0
        self._part_base_bytes = 0
        self._cached_speed = 0.0
        self._cached_eta = 0

        self._write_task_progress(0, self._grand_total_bytes, 0.0, 0, 0, 1, n_parts)

        results = []
        try:
            for part_idx, (part_path, part_name) in enumerate(parts, start=1):
                self.upload_progress["current_part"] = part_idx

                caption = self._dump_caption(task_id, user_id, part_name, part_idx, n_parts)

                result = await self._send_part_with_flood_retry(
                    upload_chat_id=upload_chat_id,
                    part_path=part_path,
                    part_name=part_name,
                    thumb=thumb,
                    caption=caption,
                    send_type=send_type,
                )

                results.append(result)
                self._part_base_bytes += os.path.getsize(part_path)

            self.upload_progress["status"] = "completed"
            self._write_task_progress(
                self._grand_total_bytes, self._grand_total_bytes,
                100.0, self._cached_speed, 0, n_parts, n_parts,
            )
            if self.task_queue:
                self.task_queue.update_status(task_id, "uploading", 100)

            return results

        except asyncio.CancelledError:
            self.upload_progress["status"] = "cancelled"
            raise
        except Exception as e:
            self.upload_progress["status"] = "failed"
            raise Exception(f"Upload failed: {e}") from e

    async def _send_part_with_flood_retry(
        self,
        *,
        upload_chat_id,
        part_path: str,
        part_name: str,
        thumb,
        caption: str,
        send_type: str,
    ):
        def _on_wait(_delay: int, _attempt: int) -> None:
            self.upload_progress["status"] = "rate_limited"

        # Hand the local path straight to Kurigram/Pyrogram's native
        # send_document/send_video. They call the client's own save_file()
        # internally, which performs the real chunking and concurrent
        # MTProto transmissions (bounded by the Client's
        # max_concurrent_transmissions) -- no manual SaveBigFilePart/
        # InputFileBig construction needed here at all.
        if send_type.lower() in ["doc", "document"]:
            return await call_with_flood_retry(
                self.client.send_document,
                chat_id=upload_chat_id,
                document=part_path,
                thumb=thumb,
                caption=caption,
                force_document=True,
                file_name=part_name,
                progress=self._progress_callback,
                max_retries=MAX_FLOOD_WAIT_RETRIES,
                on_wait=_on_wait,
            )

        return await call_with_flood_retry(
            self.client.send_video,
            chat_id=upload_chat_id,
            video=part_path,
            thumb=thumb,
            caption=caption,
            file_name=part_name,
            supports_streaming=True,
            progress=self._progress_callback,
            max_retries=MAX_FLOOD_WAIT_RETRIES,
            on_wait=_on_wait,
        )

    async def _progress_callback(self, current: int, total: int):
        # Called directly by Kurigram/Pyrogram's save_file() with the real
        # upload byte counter for whichever part is transmitting right now,
        # so the displayed speed always reflects the actual upload callback
        # values -- nothing here is derived from manually generated chunks.
        now = time.time()

        overall_uploaded = min(self._part_base_bytes + current, self._grand_total_bytes)
        overall_pct = (
            overall_uploaded / self._grand_total_bytes * 100
        ) if self._grand_total_bytes else 0.0
        overall_pct = round(overall_pct, 2)

        async with self._progress_lock:
            if self._last_time is None:
                self._last_time = now
                self._last_bytes = overall_uploaded
                self.upload_progress.update({
                    "uploaded": overall_uploaded,
                    "percentage": overall_pct,
                    "speed": 0.0,
                    "eta": 0,
                    "elapsed": 0,
                })
                self._write_task_progress(
                    overall_uploaded, self._grand_total_bytes,
                    overall_pct, 0.0, 0,
                    self.upload_progress.get("current_part", 1),
                    self.upload_progress.get("total_parts", 1),
                )
                if self.task_queue:
                    self.task_queue.update_status(
                        self.task_data["task_id"], "uploading", overall_pct
                    )
                return

            interval = now - self._last_time
            if interval >= _SPEED_UPDATE_INTERVAL:
                bytes_delta = overall_uploaded - self._last_bytes
                self._cached_speed = max(0.0, bytes_delta / interval)
                remaining = self._grand_total_bytes - overall_uploaded
                self._cached_eta = (
                    int(remaining / self._cached_speed)
                    if self._cached_speed > 0 else 0
                )
                self._last_time = now
                self._last_bytes = overall_uploaded

            total_elapsed = int(now - self._start_time) if self._start_time else 0

            self.upload_progress.update({
                "uploaded": overall_uploaded,
                "percentage": overall_pct,
                "speed": round(self._cached_speed, 2),
                "eta": self._cached_eta,
                "elapsed": total_elapsed,
            })

            if self.task_queue:
                self.task_queue.update_status(
                    self.task_data["task_id"], "uploading", overall_pct
                )

            self._write_task_progress(
                overall_uploaded, self._grand_total_bytes,
                overall_pct, round(self._cached_speed, 2), self._cached_eta,
                self.upload_progress["current_part"],
                self.upload_progress["total_parts"],
            )

    def _write_task_progress(
        self,
        uploaded: int,
        total_size: int,
        percentage: float,
        speed: float,
        eta: int,
        current_part: int = 1,
        total_parts: int = 1,
    ):
        if not self.task_queue:
            return
        task = self.task_queue.tasks.get(self.task_data["task_id"])
        if task is not None:
            task["upload_progress"] = {
                "total_size": total_size,
                "uploaded": uploaded,
                "percentage": percentage,
                "speed": speed,
                "eta": eta,
                "current_part": current_part,
                "total_parts": total_parts,
            }

    async def _split_file(
        self,
        file_path: str,
        max_bytes: int,
        output_name: str,
        task_folder: str,
    ) -> list[tuple[str, str]]:
        base, ext = os.path.splitext(output_name)
        return await self._split_bytes(
            file_path, max_bytes, base, ext, task_folder
        )

    async def _split_bytes(
        self,
        file_path: str,
        max_bytes: int,
        base: str,
        ext: str,
        task_folder: str,
    ) -> list[tuple[str, str]]:
        READ_BUF = 8 * 1024 * 1024
        parts = []
        idx = 1

        with open(file_path, "rb") as src:
            while True:
                part_name = f"{base} P{idx}{ext}"
                part_path = os.path.join(task_folder, part_name)
                written = 0

                with open(part_path, "wb") as dst:
                    while written < max_bytes:
                        chunk = src.read(min(READ_BUF, max_bytes - written))
                        if not chunk:
                            break
                        dst.write(chunk)
                        written += len(chunk)

                if written == 0:
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                    break

                parts.append((part_path, part_name))
                idx += 1

                if written < max_bytes:
                    break

        return parts

    def get_progress(self) -> dict:
        return self.upload_progress.copy()
