"""Synchronous MongoDB persistence for per-user rename settings and assets."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from gridfs import GridFSBucket
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError


class UserSettingsStoreError(RuntimeError):
    """Raised when user settings cannot be read from or written to MongoDB."""


class UserSettingsStore:
    """Stores settings documents and user thumbnail/font assets in one database."""

    def __init__(self, mongo_uri: str, database_name: str):
        self._client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5_000)
        database = self._client[database_name]
        self._settings = database["user_settings"]
        self._assets = GridFSBucket(database, bucket_name="user_assets")

    def initialize(self) -> None:
        try:
            self._client.admin.command("ping")
            self._settings.create_index([("updated_at", ASCENDING)])
        except PyMongoError as exc:
            raise UserSettingsStoreError("Unable to initialize MongoDB user settings.") from exc

    def load(self, user_id: int) -> dict[str, Any] | None:
        try:
            document = self._settings.find_one({"_id": user_id}, {"settings": 1})
        except PyMongoError as exc:
            raise UserSettingsStoreError("Unable to read user settings from MongoDB.") from exc
        settings = (document or {}).get("settings")
        return deepcopy(settings) if isinstance(settings, dict) else None

    def save(self, user_id: int, settings: dict[str, Any]) -> None:
        try:
            self._settings.update_one(
                {"_id": user_id},
                {
                    "$set": {
                        "settings": deepcopy(settings),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except PyMongoError as exc:
            raise UserSettingsStoreError("Unable to save user settings to MongoDB.") from exc

    def upload_asset(self, user_id: int, kind: str, path: str, previous_id: str = "") -> str:
        """Upload an asset and remove its prior GridFS version when possible."""
        try:
            with open(path, "rb") as source:
                asset_id = self._assets.upload_from_stream(
                    f"{kind}_{user_id}_{Path(path).name}",
                    source,
                    metadata={"user_id": user_id, "kind": kind},
                )
            self.delete_asset(previous_id)
            return str(asset_id)
        except (OSError, PyMongoError) as exc:
            raise UserSettingsStoreError(f"Unable to store user {kind} in MongoDB.") from exc

    def restore_asset(self, asset_id: str, destination: str) -> bool:
        if not asset_id:
            return False
        try:
            object_id = ObjectId(asset_id)
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            with open(destination, "wb") as target:
                self._assets.download_to_stream(object_id, target)
            return True
        except (OSError, PyMongoError, ValueError):
            return False

    def delete_asset(self, asset_id: str) -> None:
        if not asset_id:
            return
        try:
            self._assets.delete(ObjectId(asset_id))
        except (PyMongoError, ValueError):
            # Missing or malformed historic asset IDs should not block settings changes.
            pass

    def close(self) -> None:
        self._client.close()
