"""Mongo-backed administrator access control.

``OWNER_IDS`` are bootstrap owners. They remain available if MongoDB is reset,
while day-to-day administrators are stored in MongoDB and managed by commands.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
from pymongo.errors import PyMongoError

from .task_store import TaskStore, TaskStoreError


logger = logging.getLogger(__name__)


class WorkerStoreError(RuntimeError):
    """Raised when MongoDB cannot complete a worker-store operation."""


class AccessControl:
    """Checks bootstrap owners and MongoDB-managed administrators."""

    def __init__(self, owner_ids: Iterable[int], mongo_uri: str, database_name: str = "renamer_bot"):
        self.owner_ids = frozenset(owner_ids)
        self._client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5_000)
        database = self._client[database_name]
        self._admins = database["admins"]
        self._legacy_workers = database["workers"]
        self.task_store = TaskStore(self._client, database_name)

    async def initialize(self) -> None:
        """Verify the connection and protect against duplicate worker IDs."""
        try:
            await self._client.admin.command("ping")
            await self._admins.create_index([("admin_id", ASCENDING)], unique=True)
            # Preserve access granted by older versions that used `workers`.
            legacy_workers = await self._legacy_workers.find(
                {}, {"_id": 0, "worker_id": 1, "added_by": 1, "added_at": 1}
            ).to_list(length=None)
            for record in legacy_workers:
                worker_id = record.get("worker_id")
                if not isinstance(worker_id, int) or worker_id <= 0:
                    continue
                await self._admins.update_one(
                    {"admin_id": worker_id},
                    {
                        "$setOnInsert": {
                            "admin_id": worker_id,
                            "added_by": record.get("added_by", 0),
                            "added_at": record.get("added_at", datetime.now(timezone.utc)),
                            "bootstrap": False,
                        }
                    },
                    upsert=True,
                )
            if self.owner_ids:
                now = datetime.now(timezone.utc)
                for owner_id in self.owner_ids:
                    await self._admins.update_one(
                        {"admin_id": owner_id},
                        {
                            "$setOnInsert": {
                                "admin_id": owner_id,
                                "added_by": owner_id,
                                "added_at": now,
                                "bootstrap": True,
                            }
                        },
                        upsert=True,
                    )
            await self.task_store.initialize()
        except PyMongoError as exc:
            raise WorkerStoreError("Unable to connect to MongoDB for worker access.") from exc
        except TaskStoreError as exc:
            raise WorkerStoreError(str(exc)) from exc

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    async def is_authorized(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            return True
        try:
            return await self._admins.find_one({"admin_id": user_id}) is not None
        except PyMongoError as exc:
            logger.warning("Administrator access check failed: %s", exc)
            return False

    async def add_admin(self, admin_id: int, added_by: int) -> bool:
        """Add an administrator, returning ``True`` only when newly added."""
        try:
            result = await self._admins.update_one(
                {"admin_id": admin_id},
                {
                    "$setOnInsert": {
                        "admin_id": admin_id,
                        "added_by": added_by,
                        "added_at": datetime.now(timezone.utc),
                        "bootstrap": False,
                    }
                },
                upsert=True,
            )
            return result.upserted_id is not None
        except PyMongoError as exc:
            raise WorkerStoreError("Could not add the administrator in MongoDB.") from exc

    async def remove_admin(self, admin_id: int) -> bool:
        try:
            result = await self._admins.delete_one({"admin_id": admin_id, "bootstrap": {"$ne": True}})
            return result.deleted_count > 0
        except PyMongoError as exc:
            raise WorkerStoreError("Could not remove the administrator from MongoDB.") from exc

    async def list_admins(self) -> list[int]:
        try:
            records = await self._admins.find({}, {"_id": 0, "admin_id": 1}).sort(
                "admin_id", ASCENDING
            ).to_list(length=None)
            return [record["admin_id"] for record in records]
        except PyMongoError as exc:
            raise WorkerStoreError("Could not read administrators from MongoDB.") from exc

    # Backward-compatible method names used by older deployments.
    add_worker = add_admin
    remove_worker = remove_admin
    list_workers = list_admins

    def close(self) -> None:
        self._client.close()
