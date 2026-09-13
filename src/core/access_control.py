
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from .task_store import TaskStore, TaskStoreError


logger = logging.getLogger(__name__)


class WorkerStoreError(RuntimeError):
    pass


class AccessControl:

    def __init__(self, owner_ids: Iterable[int], mongo_uri: str, database_name: str = "renamer_bot"):
        self.owner_ids = frozenset(owner_ids)
        self._client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5_000)
        database = self._client[database_name]
        self._admins = database["admins"]
        self._legacy_workers = database["workers"]
        self._premium_users = database["premium_users"]
        self._banned_users = database["banned_users"]
        self.task_store = TaskStore(self._client, database_name)

    async def initialize(self) -> None:
        try:
            await self._client.admin.command("ping")
            await self._admins.create_index([("admin_id", ASCENDING)], unique=True)
            await self._premium_users.create_index([("user_id", ASCENDING)], unique=True)
            await self._banned_users.create_index([("user_id", ASCENDING)], unique=True)
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

    add_worker = add_admin
    remove_worker = remove_admin
    list_workers = list_admins


    async def add_premium(
        self,
        user_id: int,
        added_by: int,
        plan: str = "Standard",
        duration_days: int | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=duration_days) if duration_days else None
        try:
            result = await self._premium_users.update_one(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "added_by": added_by,
                        "added_at": now,
                        "plan": plan,
                        "expires_at": expires_at,
                    }
                },
                upsert=True,
            )
            return result.upserted_id is not None
        except PyMongoError as exc:
            raise WorkerStoreError("Could not add the Premium user in MongoDB.") from exc

    async def remove_premium(self, user_id: int) -> bool:
        try:
            result = await self._premium_users.delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except PyMongoError as exc:
            raise WorkerStoreError("Could not remove the Premium user from MongoDB.") from exc

    async def list_premium(self) -> list[int]:
        try:
            records = await self._premium_users.find({}, {"_id": 0, "user_id": 1}).sort(
                "user_id", ASCENDING
            ).to_list(length=None)
            return [record["user_id"] for record in records]
        except PyMongoError as exc:
            raise WorkerStoreError("Could not read Premium users from MongoDB.") from exc

    async def list_premium_records(self) -> list[dict]:
        try:
            return await self._premium_users.find({}, {"_id": 0}).sort(
                "added_at", DESCENDING
            ).to_list(length=None)
        except PyMongoError as exc:
            raise WorkerStoreError("Could not read Premium users from MongoDB.") from exc

    async def get_premium(self, user_id: int) -> dict | None:
        try:
            return await self._premium_users.find_one({"user_id": user_id}, {"_id": 0})
        except PyMongoError as exc:
            logger.warning("Premium record lookup failed: %s", exc)
            return None

    async def is_premium(self, user_id: int) -> bool:
        record = await self.get_premium(user_id)
        if not record:
            return False
        expires_at = record.get("expires_at")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return False
        return True

    async def can_use_premium_features(self, user_id: int) -> bool:
        if await self.is_banned(user_id):
            return False
        if self.is_owner(user_id):
            return True
        return await self.is_premium(user_id)


    async def ban_user(self, user_id: int, banned_by: int, reason: str = "") -> bool:
        try:
            result = await self._banned_users.update_one(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "banned_by": banned_by,
                        "banned_at": datetime.now(timezone.utc),
                        "reason": reason or "",
                    }
                },
                upsert=True,
            )
            return result.upserted_id is not None
        except PyMongoError as exc:
            raise WorkerStoreError("Could not ban the user in MongoDB.") from exc

    async def unban_user(self, user_id: int) -> bool:
        try:
            result = await self._banned_users.delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except PyMongoError as exc:
            raise WorkerStoreError("Could not unban the user from MongoDB.") from exc

    async def list_banned(self) -> list[dict]:
        try:
            return await self._banned_users.find({}, {"_id": 0}).sort(
                "banned_at", DESCENDING
            ).to_list(length=None)
        except PyMongoError as exc:
            raise WorkerStoreError("Could not read banned users from MongoDB.") from exc

    async def get_ban(self, user_id: int) -> dict | None:
        try:
            return await self._banned_users.find_one({"user_id": user_id}, {"_id": 0})
        except PyMongoError as exc:
            logger.warning("Ban record lookup failed: %s", exc)
            return None

    async def is_banned(self, user_id: int) -> bool:
        try:
            return await self._banned_users.find_one({"user_id": user_id}) is not None
        except PyMongoError as exc:
            logger.warning("Ban check failed: %s", exc)
            return False

    def close(self) -> None:
        self._client.close()
