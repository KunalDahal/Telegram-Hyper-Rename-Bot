
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bson import ObjectId
from gridfs import GridFSBucket
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)


class UserSettingsStoreError(RuntimeError):
    pass


class UserSettingsStore:

    def __init__(self, mongo_uri: str, database_name: str):
        self._client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5_000)
        database = self._client[database_name]
        self._settings = database["user_settings"]
        self._assets = GridFSBucket(database, bucket_name="user_assets")
        # pymongo/GridFS are synchronous over the network. UserSettings is
        # called directly from async Pyrogram handlers, so every call here
        # used to block the event loop for however long Mongo took to
        # respond. A single worker thread serializes all writes for a given
        # process (so e.g. an asset upload always finishes before a settings
        # save that references its id, without extra locking) while keeping
        # them off the event loop. Reads that a caller needs synchronously
        # (load/restore_asset at startup or cold-cache time) still go
        # through the plain blocking methods below.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="user-settings-io")

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

    def upload_asset(self, user_id: int, kind: str, path: str) -> str:
        # NOTE: this intentionally does NOT delete any previous asset. If it
        # did, and the caller's subsequent settings save then failed, the
        # settings document would keep pointing at an asset id that no
        # longer exists in GridFS - a real bug this file used to have.
        # Callers should only delete a superseded asset once they've
        # confirmed the document referencing the *new* one was saved.
        try:
            with open(path, "rb") as source:
                asset_id = self._assets.upload_from_stream(
                    f"{kind}_{user_id}_{Path(path).name}",
                    source,
                    metadata={"user_id": user_id, "kind": kind},
                )
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
            pass

    def save_async(
        self,
        user_id: int,
        settings: dict[str, Any],
        callback: Callable[[bool], None] | None = None,
    ) -> None:
        """Persist `settings` on the background worker thread instead of the
        calling (event loop) thread. `callback`, if given, is invoked from
        the worker thread once the write finishes, with True/False for
        success/failure - never re-raises into the caller.
        """
        data_copy = deepcopy(settings)

        def _job() -> None:
            ok = True
            try:
                self.save(user_id, data_copy)
            except UserSettingsStoreError:
                logger.exception("Background settings save failed for user %s", user_id)
                ok = False
            if callback is not None:
                try:
                    callback(ok)
                except Exception:
                    logger.exception("save_async callback raised for user %s", user_id)

        self._executor.submit(_job)

    def upload_asset_async(
        self,
        user_id: int,
        kind: str,
        path: str,
        callback: Callable[[bool, str], None],
    ) -> None:
        """Upload an asset on the background worker thread. `callback` is
        invoked from that same worker thread with (success, asset_id) -
        asset_id is "" on failure. Because this store uses a single worker
        thread, a save_async() submitted afterwards (e.g. from within the
        callback) is guaranteed to run after this upload completes.
        """

        def _job() -> None:
            try:
                asset_id = self.upload_asset(user_id, kind, path)
                callback(True, asset_id)
            except UserSettingsStoreError:
                logger.exception(
                    "Background asset upload failed for user %s (kind=%s)", user_id, kind
                )
                callback(False, "")

        self._executor.submit(_job)

    def delete_asset_async(self, asset_id: str) -> None:
        """Delete an asset on the background worker thread. Fire-and-forget -
        delete_asset() already swallows its own errors."""
        if not asset_id:
            return
        self._executor.submit(self.delete_asset, asset_id)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._client.close()
