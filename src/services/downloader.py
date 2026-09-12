import os
import asyncio
import time
import logging

from src.services.hyper_downloader import HyperTGDownloader


MAX_DOWNLOAD_ATTEMPTS = 3


class Downloader:
    """Thin task-level adapter around HyperTGDownloader.

    HyperTG owns the actual Telegram download, parallel chunk workers,
    temporary part storage, final assembly, retries, and exact-size checks.
    This class only resolves the staged/source message and reports progress
    back to TaskQueue.
    """

    def __init__(self, temp_base: str, task_queue=None, task_id=None, helper_bots=None, helper_loads=None):
        self.temp_base = temp_base
        self.task_queue = task_queue
        self.task_id = task_id
        self.helper_bots = helper_bots or {}
        self.helper_loads = helper_loads or {}
        self._start_time = None
        self._last_cb_time = None
        self._last_cb_bytes = 0
        self._declared_size = 0
        os.makedirs(self.temp_base, exist_ok=True)

        self.download_progress = {
            "total_size": 0,
            "downloaded": 0,
            "percentage": 0,
            "speed": 0,
            "eta": 0,
            "elapsed": 0,
            "status": "idle",
        }

    async def download(self, client, task_data: dict) -> str:
        task_id = task_data["task_id"]
        self.task_id = task_id
        self._declared_size = int(task_data.get("file_size") or 0)
        source_chat_id = task_data.get("download_source_chat_id") or task_data.get("source_chat_id")
        source_message_id = task_data.get("download_source_message_id") or task_data.get("source_message_id")
        original_file_name = task_data.get("original_file_name") or f"video_{task_id}.mkv"

        task_folder = os.path.join(self.temp_base, task_id)
        os.makedirs(task_folder, exist_ok=True)
        desired_path = os.path.abspath(os.path.join(task_folder, original_file_name))

        self._reset_progress()
        self.download_progress["status"] = "downloading"
        self._start_time = time.time()

        if not source_chat_id or not source_message_id:
            raise Exception("Download source chat/message ID is missing")

        try:
            media_source = await client.get_messages(
                chat_id=source_chat_id,
                message_ids=source_message_id,
            )
            if not media_source or getattr(media_source, "empty", False):
                raise Exception(f"Source message not found: {source_chat_id}/{source_message_id}")

            if not self.helper_bots:
                raise RuntimeError("HyperTG helper bots are not configured")

            # HyperTG owns the complete download lifecycle and final file path.
            # No external packet staging/combining is performed here.
            hyper_downloader = HyperTGDownloader(
                self.helper_bots,
                self.helper_loads,
                download_dir=self.temp_base,
                logger=logging.getLogger("hyper_downloader").info,
            )

            actual_path = await hyper_downloader.download_media(
                media_source,
                file_name=desired_path,
                progress=self._progress_callback,
            )

            actual_path = self._validate_download(actual_path)
            if os.path.abspath(actual_path) != desired_path:
                os.replace(actual_path, desired_path)
                actual_path = desired_path

            completed_size = os.path.getsize(actual_path)
            self.download_progress.update({
                "total_size": completed_size,
                "downloaded": completed_size,
                "percentage": 100,
                "speed": 0,
                "eta": 0,
                "status": "completed",
            })
            if self.task_queue and self.task_id:
                task = self.task_queue.tasks.get(self.task_id)
                if task is not None:
                    task["progress_details"] = dict(self.download_progress)
                    task["progress"] = 100.0
                    task["downloaded_path"] = actual_path
                    task["download_completed"] = True
                    self.task_queue.checkpoint(self.task_id)
            return actual_path

        except asyncio.CancelledError:
            self.download_progress["status"] = "cancelled"
            raise
        except Exception as e:
            self.download_progress["status"] = "failed"
            raise Exception(f"Download failed: {e}") from e

    def _validate_download(self, actual_path) -> str:
        if not actual_path:
            raise Exception("HyperTG did not return a download path")
        actual_path = os.path.abspath(actual_path)
        if not os.path.isfile(actual_path):
            raise Exception(f"File not found after HyperTG download: {actual_path}")
        actual_size = os.path.getsize(actual_path)
        if actual_size == 0:
            raise Exception(f"Downloaded file is empty: {actual_path}")
        if self._declared_size and actual_size != self._declared_size:
            raise Exception(
                f"Downloaded file size mismatch: expected {self._declared_size} bytes, "
                f"got {actual_size} bytes"
            )
        return actual_path

    async def _progress_callback(self, current: int, total: int):
        now = time.time()
        if not total:
            total = self._declared_size
        if total and self.download_progress["total_size"] == 0:
            self.download_progress["total_size"] = total

        elapsed = max(1.0, now - self._start_time) if self._start_time else 1.0
        interval = (now - self._last_cb_time) if self._last_cb_time else 0
        if self._last_cb_time is None or interval >= 0.5:
            speed = max(0.0, (current - self._last_cb_bytes) / interval) if interval > 0 else 0.0
            self._last_cb_time = now
            self._last_cb_bytes = current
            self.download_progress["speed"] = speed

        percentage = (current / total * 100) if total else 0.0
        speed = self.download_progress.get("speed", 0.0)
        remaining = max(0, total - current) if total else 0
        eta = int(remaining / speed) if speed > 0 else 0
        self.download_progress.update({
            "downloaded": current,
            "percentage": min(100.0, percentage),
            "eta": eta,
            "elapsed": int(elapsed),
        })

        if self.task_queue and self.task_id:
            task = self.task_queue.tasks.get(self.task_id)
            if task is not None:
                task["progress_details"] = dict(self.download_progress)
                task["progress"] = min(100.0, percentage)
                self.task_queue.checkpoint(self.task_id)


    def _reset_progress(self):
        self.download_progress.update({
            "total_size": 0,
            "downloaded": 0,
            "percentage": 0,
            "speed": 0,
            "eta": 0,
            "elapsed": 0,
            "status": "idle",
        })
        self._last_cb_time = None
        self._last_cb_bytes = 0
