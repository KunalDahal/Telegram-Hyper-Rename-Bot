import asyncio
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime

from src.utils.dc_checker import get_file_dc

_ACTIVE_STATUSES = frozenset({
    "starting", "queued", "staging_to_dump", "waiting_for_download", "downloading",
    "ready", "waiting_for_processing", "processing",
    "waiting_for_upload", "uploading",
})


class TaskQueue:
    def __init__(self, task_store=None):
        self.queue:        list[str]       = []
        self.pending_queue = deque()
        self.tasks:        dict[str, dict] = {}
        self.lock          = asyncio.Lock()
        self.processing    = False
        self.current_task: str | None      = None
        self.task_store = task_store
        self._store_jobs: set[asyncio.Task] = set()
        self._checkpoint_chains: dict[str, asyncio.Task] = {}

    def set_task_store(self, task_store) -> None:
        self.task_store = task_store

    def _schedule_store(self, coro) -> None:
        """Persist asynchronously without blocking Telegram progress callbacks."""
        if not self.task_store:
            return
        try:
            job = asyncio.get_running_loop().create_task(coro)
            self._store_jobs.add(job)
            job.add_done_callback(self._store_jobs.discard)
        except RuntimeError:
            # The bot only mutates this queue while its event loop is running.
            pass

    def checkpoint(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or not self.task_store:
            return

        # Progress callbacks can schedule checkpoints faster than MongoDB can
        # complete them. Chain checkpoints for the SAME task so an older state
        # can never finish after a newer state and overwrite it. Different tasks
        # remain independent and may persist concurrently.
        snapshot = deepcopy(task)
        previous = self._checkpoint_chains.get(task_id)

        async def persist_after_previous() -> None:
            if previous:
                try:
                    await previous
                except Exception:
                    # A failed older checkpoint must not block the newest state.
                    pass
            await self.task_store.save_active(snapshot)

        try:
            job = asyncio.get_running_loop().create_task(persist_after_previous())
        except RuntimeError:
            return

        self._store_jobs.add(job)
        self._checkpoint_chains[task_id] = job

        def _done(completed: asyncio.Task) -> None:
            self._store_jobs.discard(completed)
            if self._checkpoint_chains.get(task_id) is completed:
                self._checkpoint_chains.pop(task_id, None)

        job.add_done_callback(_done)

    def create_task(self, task_data: dict) -> str:
        task_id = uuid.uuid4().hex
        file_id = task_data.get("file_id", "")
        dc      = get_file_dc(file_id) if file_id else None

        task = {
            "task_id":    task_id,
            "created_at": datetime.utcnow().isoformat(),
            "started_at": None,
            "status":     "queued",
            "progress":   0,
            "dc":         dc,
        }
        task.update(task_data)
        self.tasks[task_id] = task
        self.queue.append(task_id)
        self.pending_queue.append(task_id)
        self.checkpoint(task_id)
        return task_id

    def restore_task(self, task_data: dict) -> bool:
        """Restore an unfinished Mongo checkpoint as a queued task."""
        task_id = str(task_data.get("task_id") or "")
        if not task_id or task_id in self.tasks:
            return False

        task = dict(task_data)
        task.pop("_id", None)
        task.pop("active", None)
        task["recovered_from_status"] = task.get("status", "queued")
        task["last_known_progress"] = task.get("progress", 0)
        task["status"] = "queued"
        task["progress"] = 0
        task.pop("upload_progress", None)
        task.pop("progress_details", None)
        self.tasks[task_id] = task
        self.queue.append(task_id)
        self.pending_queue.append(task_id)
        return True

    def get_task(self, task_id: str) -> dict | None:
        return self.tasks.get(task_id)

    def pop_next_queued_task(self) -> dict | None:
        """Pop the next waiting task without scanning the full task list."""
        while self.pending_queue:
            task_id = self.pending_queue.popleft()
            task = self.tasks.get(task_id)
            if task and task.get("status") == "queued":
                return task
        return None

    def update_status(self, task_id: str, status: str, progress: int | float | None = None):
        task = self.tasks.get(task_id)
        if task is None:
            return
        task["status"] = status
        if progress is not None:
            task["progress"] = progress
        if status in ("downloading", "processing", "uploading") and not task.get("started_at"):
            task["started_at"] = datetime.utcnow().isoformat()
        self._refresh_processing_flag()
        self.checkpoint(task_id)

    def remove_task(self, task_id: str, final_status: str | None = None, error: str = ""):
        task = self.tasks.pop(task_id, None)
        try:
            self.queue.remove(task_id)
        except ValueError:
            pass
        try:
            self.pending_queue.remove(task_id)
        except ValueError:
            pass
        if self.current_task == task_id:
            self.current_task = None
        self._refresh_processing_flag()
        if task and final_status and self.task_store:
            snapshot = deepcopy(task)
            pending_writes = list(self._store_jobs)

            async def archive_after_checkpoints() -> None:
                if pending_writes:
                    await asyncio.gather(*pending_writes, return_exceptions=True)
                await self.task_store.archive(snapshot, final_status, error)

            self._schedule_store(archive_after_checkpoints())

    def get_queue_position(self, task_id: str) -> int:
        if task_id == self.current_task:
            return 0

        position = 1
        for tid in self.queue:
            if tid == self.current_task:
                continue
            task = self.tasks.get(tid)
            if task and task.get("status") in _ACTIVE_STATUSES:
                if tid == task_id:
                    return position
                position += 1
        return 0

    def get_next_queue_position(self, task_id: str) -> int:
        return self.get_queue_position(task_id)

    def shift_task(self, task_id: str, next_position: int) -> tuple[bool, str, int]:
        task = self.tasks.get(task_id)
        if not task:
            return False, "Task not found.", 0
        if task_id not in self.queue:
            return False, "Task is not in the queue.", 0
        if task_id == self.current_task:
            return False, "The currently running task cannot be shifted.", 0
        current_position = self.get_next_queue_position(task_id)
        if current_position == 1:
            return False, "Task 1 is locked as the next processing slot.", 0
        if task.get("status") not in {"queued", "ready"}:
            return False, "Only waiting or prefetched tasks can be shifted.", 0
        if int(next_position) < 2:
            return False, "Positions 0 and 1 are locked. Use position 2 or higher.", 0

        next_position = int(next_position)
        self.queue.remove(task_id)

        current_id = self.current_task if self.current_task in self.queue else None
        first_shiftable_index = self.queue.index(current_id) + 1 if current_id else 0
        waiting_ids = [
            tid for tid in self.queue
            if tid != current_id
            and self.tasks.get(tid)
            and self.tasks[tid].get("status") in _ACTIVE_STATUSES
        ]

        next_position = min(next_position, len(waiting_ids) + 1)
        if next_position <= len(waiting_ids):
            insert_at = self.queue.index(waiting_ids[next_position - 1])
        elif waiting_ids:
            insert_at = self.queue.index(waiting_ids[-1]) + 1
        elif current_id:
            insert_at = self.queue.index(current_id) + 1
        else:
            insert_at = 0

        if current_id:
            insert_at = max(insert_at, first_shiftable_index)

        self.queue.insert(insert_at, task_id)
        self.pending_queue = deque(
            tid for tid in self.queue
            if self.tasks.get(tid, {}).get("status") == "queued"
        )
        return True, "Task shifted.", next_position

    def purge_stale_tasks(self):
        _in_progress = frozenset({
            "starting", "staging_to_dump", "waiting_for_download", "downloading", "ready",
            "waiting_for_processing", "processing",
            "waiting_for_upload", "uploading",
        })
        for task in self.tasks.values():
            if task.get("status") in _in_progress:
                task["recovered_from_status"] = task.get("status")
                task["last_known_progress"] = task.get("progress", 0)
                task["status"] = "queued"
                task["progress"] = 0
                task.pop("_downloaded_path", None)
                task.pop("upload_progress", None)
                task.pop("progress_details", None)
                self.checkpoint(task["task_id"])

        queue_set = set(self.queue)
        stale = [
            tid for tid, task in list(self.tasks.items())
            if tid not in queue_set or task.get("status") not in _ACTIVE_STATUSES
        ]
        for tid in stale:
            self.tasks.pop(tid, None)
            try:
                self.queue.remove(tid)
            except ValueError:
                pass

        self.queue = [tid for tid in self.queue if tid in self.tasks]
        self.pending_queue = deque(
            tid for tid in self.queue
            if self.tasks.get(tid, {}).get("status") == "queued"
        )
        if self.current_task and self.current_task not in self.tasks:
            self.current_task = None
        self._refresh_processing_flag()

    def clear(self) -> None:
        self.queue.clear()
        self.pending_queue.clear()
        self.tasks.clear()
        self.current_task = None
        self.processing = False
        self._checkpoint_chains.clear()

    async def flush_checkpoints(self) -> None:
        if self._store_jobs:
            await asyncio.gather(*list(self._store_jobs), return_exceptions=True)

    def is_processing(self) -> bool:
        return self.processing

    def get_current_task(self) -> dict | None:
        return self.tasks.get(self.current_task) if self.current_task else None

    def _refresh_processing_flag(self):
        self.processing = any(
            t.get("status") in (_ACTIVE_STATUSES - {"queued"})
            for t in self.tasks.values()
        )
