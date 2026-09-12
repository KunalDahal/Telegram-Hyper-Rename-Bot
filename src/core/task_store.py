"""Durable MongoDB storage for rename-task checkpoints and history."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Iterable

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError


class TaskStoreError(RuntimeError):
    """Raised when a task checkpoint cannot be saved or restored."""


class TaskStore:
    """Persists active tasks so an unplanned process restart can recover them."""

    def __init__(self, client: AsyncIOMotorClient, database_name: str = "renamer_bot"):
        database = client[database_name]
        self._tasks = database["rename_tasks"]

    async def initialize(self) -> None:
        try:
            await self._tasks.create_index([("task_id", ASCENDING)], unique=True)
            await self._tasks.create_index([("active", ASCENDING), ("created_at", ASCENDING)])
            await self._tasks.create_index([("finished_at", DESCENDING)])
        except PyMongoError as exc:
            raise TaskStoreError("Unable to initialize MongoDB task storage.") from exc

    async def save_active(self, task: dict) -> None:
        """Store the latest task state and progress checkpoint."""
        task_id = task.get("task_id")
        if not task_id:
            return
        document = deepcopy(task)
        document.pop("_id", None)
        document["active"] = True
        document["updated_at"] = datetime.now(timezone.utc)
        try:
            await self._tasks.replace_one({"task_id": task_id}, document, upsert=True)
        except PyMongoError as exc:
            raise TaskStoreError(f"Unable to save task {task_id}.") from exc

    async def load_active(self) -> list[dict]:
        try:
            records = await self._tasks.find({"active": True}, {"_id": 0}).sort(
                "created_at", ASCENDING
            ).to_list(length=None)
            return records
        except PyMongoError as exc:
            raise TaskStoreError("Unable to restore queued tasks from MongoDB.") from exc

    async def archive(self, task: dict, final_status: str, error: str = "") -> None:
        task_id = task.get("task_id")
        if not task_id:
            return
        document = deepcopy(task)
        document.pop("_id", None)
        document.update({
            "active": False,
            "status": final_status,
            "finished_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        if error:
            document["error"] = error[:500]
        try:
            await self._tasks.replace_one({"task_id": task_id}, document, upsert=True)
        except PyMongoError as exc:
            raise TaskStoreError(f"Unable to archive task {task_id}.") from exc

    async def clear_all(self) -> int:
        """Remove active and historical task records, but never administrator data."""
        try:
            result = await self._tasks.delete_many({})
            return result.deleted_count
        except PyMongoError as exc:
            raise TaskStoreError("Unable to clear MongoDB task records.") from exc
