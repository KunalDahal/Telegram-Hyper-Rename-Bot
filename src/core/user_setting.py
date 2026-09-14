
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from copy import deepcopy
from typing import Any, Dict

from .user_settings_store import UserSettingsStore, UserSettingsStoreError


logger = logging.getLogger(__name__)


VALID_SEND_TYPES = {"media", "document"}

DEFAULT_METADATA = {
    "movie_name": "",
    "artist": "",
    "author": "",
    "title_all": "",
    "encoder": "",
}

DEFAULT_CAPTION_TEMPLATE = "<b>{filename}</b>"


class UserSettings:

    _temp_state: Dict[int, Dict] = {}

    def __init__(self, user_id: int, paths=None, store: UserSettingsStore | None = None):
        self.user_id = user_id
        self.store = store
        if paths is not None:
            self.db_folder = paths.users
            self.thumbnails_folder = paths.thumbnails
            self.fonts_folder = paths.fonts
        else:
            self.db_folder = "./src/bin/users"
            self.thumbnails_folder = "./src/bin/thumbnails"
            self.fonts_folder = "./src/bin/fonts"

        for folder in (self.db_folder, self.thumbnails_folder, self.fonts_folder):
            os.makedirs(folder, exist_ok=True)

        self.storage_path = os.path.join(self.db_folder, f"{self.user_id}.json")
        self.data: Dict[str, Any] = {}
        # Monotonic counter used to detect a set_thumbnail() call that has
        # been superseded by a newer one (or by clear_thumbnail()) before
        # its background upload finished - see set_thumbnail() for how
        # this is used.
        self._thumbnail_op_seq = 0
        self._load()

    @property
    def temp_state(self) -> Dict[int, Dict]:
        return UserSettings._temp_state

    def _load_legacy_file(self) -> Dict[str, Any]:
        if not os.path.exists(self.storage_path):
            return self._get_default_settings()
        try:
            with open(self.storage_path, "r", encoding="utf-8") as source:
                loaded = json.load(source)
            return loaded if isinstance(loaded, dict) else self._get_default_settings()
        except Exception:
            return self._get_default_settings()

    def _load(self) -> None:
        loaded: Dict[str, Any] | None = None
        if self.store:
            try:
                loaded = self.store.load(self.user_id)
            except UserSettingsStoreError:
                logger.exception("Could not load settings for user %s from MongoDB", self.user_id)

        is_new_mongo_record = loaded is None
        self.data = loaded if loaded is not None else self._load_legacy_file()
        self._normalize()
        if self.store and is_new_mongo_record:
            self._migrate_legacy_assets()
        self._restore_assets()
        if self.store and is_new_mongo_record:
            self._save()

    def _normalize(self) -> None:
        self.data["user_id"] = self.user_id
        metadata = self.data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if not metadata.get("title_all") and metadata.get("title"):
            metadata["title_all"] = metadata["title"]
        for key, value in DEFAULT_METADATA.items():
            metadata.setdefault(key, value)
        metadata.pop("title", None)
        self.data["metadata"] = metadata

        send_type = str(self.data.get("send_type") or "media").lower()
        self.data["send_type"] = send_type if send_type in VALID_SEND_TYPES else "media"
        self.data["auto_detect_thumb"] = self._as_bool(
            self.data.get("auto_detect_thumb", False)
        )
        self.data.setdefault("thumbnail_path", "")
        self.data.setdefault("thumbnail_asset_id", "")
        self.data["custom_caption"] = str(self.data.get("custom_caption") or "").strip()
        self.data["caption_disabled"] = self._as_bool(self.data.get("caption_disabled", False))

        self.data.pop("watermark", None)

        self.data.setdefault("format", "{title} S{season}E{episode} [{quality}] [{audio}].mkv")
        self.data["default_start_episode"] = self._positive_number_string(
            self.data.get("default_start_episode", 1)
        )
        self.data["default_season"] = self._positive_number_string(
            self.data.get("default_season", 1)
        )
        self.data.setdefault("default_audio", "SUB")
        for key in ("resolutions", "resolution", "crf", "preset", "codec", "audio_bitrate", "profiles", "params"):
            self.data.pop(key, None)

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _clamp_int(value: Any, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = minimum
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _positive_number_string(value: Any) -> str:
        text = str(value).strip()
        if not text.isdigit() or int(text) < 1:
            return "1"
        return text

    def _restore_assets(self) -> None:
        if not self.store:
            return

        thumbnail_id = str(self.data.get("thumbnail_asset_id") or "")
        if thumbnail_id:
            thumbnail_path = os.path.join(self.thumbnails_folder, f"thumb_{self.user_id}.jpg")
            if self.store.restore_asset(thumbnail_id, thumbnail_path):
                self.data["thumbnail_path"] = os.path.abspath(thumbnail_path)
            else:
                self.data["thumbnail_path"] = ""

    def _migrate_legacy_assets(self) -> None:
        if not self.store:
            return
        thumbnail_path = str(self.data.get("thumbnail_path") or "")
        if thumbnail_path and os.path.isfile(thumbnail_path) and not self.data.get("thumbnail_asset_id"):
            try:
                self.data["thumbnail_asset_id"] = self.store.upload_asset(
                    self.user_id, "thumbnail", thumbnail_path
                )
            except UserSettingsStoreError:
                logger.exception("Could not migrate thumbnail for user %s", self.user_id)

    def _save_legacy_file(self) -> None:
        try:
            with open(self.storage_path, "w", encoding="utf-8") as target:
                json.dump(self.data, target, indent=2)
        except Exception:
            logger.exception("Could not write local settings fallback for user %s", self.user_id)

    def _save(self) -> None:
        """Persist the current settings. When a Mongo store is configured,
        the network write is offloaded to the store's background thread
        (see UserSettingsStore.save_async) so this never blocks the
        caller's event loop."""
        self._save_then(None)

    def _save_then(self, on_saved) -> None:
        """Like _save(), but also calls on_saved(mongo_ok: bool) once the
        write finishes - from the background I/O thread if a store is
        configured, or immediately if it isn't. Callers that must not
        delete a superseded GridFS asset until they know the settings
        document pointing at its replacement was actually saved (see
        set_thumbnail/clear_thumbnail below) should use this instead of
        _save().
        """
        if not self.store:
            self._save_legacy_file()
            if on_saved:
                on_saved(False)
            return

        def _callback(ok: bool) -> None:
            if not ok:
                self._save_legacy_file()
            if on_saved:
                on_saved(ok)

        self.store.save_async(self.user_id, self.data, callback=_callback)

    def _get_default_settings(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "send_type": "media",
            "auto_detect_thumb": False,
            "metadata": deepcopy(DEFAULT_METADATA),
            "thumbnail_path": "",
            "thumbnail_asset_id": "",
            "format": "{title} S{season}E{episode} [{quality}] [{audio}].mkv",
            "default_start_episode": 1,
            "default_season": 1,
            "default_audio": "SUB",
            "custom_caption": "",
            "caption_disabled": False,
        }

    def get(self) -> Dict[str, Any]:
        return deepcopy(self.data)

    def update(self, key: str, value: Any) -> None:
        self.data[key] = value
        self._normalize()
        self._save()

    def reset(self) -> None:
        self._delete_assets()
        self.data = self._get_default_settings()
        self._save()

    def update_metadata(
        self,
        movie_name: str | None = None,
        artist: str | None = None,
        author: str | None = None,
        title_all: str | None = None,
        encoder: str | None = None,
    ) -> None:
        metadata = self.data["metadata"]
        values = {
            "movie_name": movie_name,
            "artist": artist,
            "author": author,
            "title_all": title_all,
            "encoder": encoder,
        }
        for key, value in values.items():
            if value is not None:
                metadata[key] = value
        self._save()

    def set_thumbnail(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        destination = os.path.abspath(os.path.join(self.thumbnails_folder, f"thumb_{self.user_id}.jpg"))
        shutil.copy2(path, destination)
        previous_asset_id = str(self.data.get("thumbnail_asset_id") or "")

        if not self.store:
            self.data["thumbnail_path"] = destination
            self._save()
            return

        # Claim this as the latest in-flight thumbnail operation. Any
        # earlier set_thumbnail()/clear_thumbnail() call whose background
        # upload/save is still pending is now stale and must not be
        # allowed to overwrite what we're about to apply.
        self._thumbnail_op_seq += 1
        my_seq = self._thumbnail_op_seq
        loop = asyncio.get_event_loop()

        def _apply_uploaded(ok: bool, new_asset_id: str) -> None:
            # Runs on the asyncio/main thread (see call_soon_threadsafe
            # below) - self.data is only ever mutated here, never from the
            # background I/O thread, so a slow/older call can't race a
            # newer one for the write.
            if my_seq != self._thumbnail_op_seq:
                # Superseded while the upload was in flight: drop the
                # result instead of reviving stale state, and don't leave
                # the freshly uploaded asset orphaned in GridFS.
                if ok and new_asset_id:
                    self.store.delete_asset_async(new_asset_id)
                return

            if ok:
                self.data["thumbnail_asset_id"] = new_asset_id
            else:
                logger.error("Could not persist thumbnail for user %s", self.user_id)
            self.data["thumbnail_path"] = destination

            def _on_saved(mongo_ok: bool) -> None:
                # Only delete the superseded GridFS asset once we know the
                # settings document pointing at its replacement actually
                # landed in Mongo - otherwise a failed save here would
                # leave Mongo pointing at an asset we've already deleted.
                if mongo_ok and ok and previous_asset_id and previous_asset_id != new_asset_id:
                    self.store.delete_asset(previous_asset_id)

            self._save_then(_on_saved)

        def _on_uploaded(ok: bool, new_asset_id: str) -> None:
            # Runs on the background I/O thread - hand the result back to
            # the main thread instead of touching self.data here.
            loop.call_soon_threadsafe(_apply_uploaded, ok, new_asset_id)

        self.store.upload_asset_async(self.user_id, "thumbnail", destination, _on_uploaded)

    def clear_thumbnail(self) -> None:
        self._remove_local_file(str(self.data.get("thumbnail_path") or ""), self.thumbnails_folder)
        previous_asset_id = str(self.data.get("thumbnail_asset_id") or "")
        # Supersede any set_thumbnail() upload still in flight so its
        # eventual result can't reapply the thumbnail we're clearing here.
        self._thumbnail_op_seq += 1
        self.data["thumbnail_asset_id"] = ""
        self.data["thumbnail_path"] = ""

        if not self.store:
            self._save()
            return

        def _on_saved(mongo_ok: bool) -> None:
            if mongo_ok and previous_asset_id:
                self.store.delete_asset(previous_asset_id)

        self._save_then(_on_saved)

    def _delete_assets(self) -> None:
        self.clear_thumbnail()

    @staticmethod
    def _remove_local_file(path: str, allowed_folder: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            if os.path.abspath(path).startswith(os.path.abspath(allowed_folder)):
                os.remove(path)
        except OSError:
            pass

    def get_format(self) -> str:
        return self.data.get("format", "{title} S{season}E{episode} [{quality}] [{audio}].mkv")

    def set_format(self, fmt: str) -> None:
        self.data["format"] = fmt
        self._save()

    def get_caption(self) -> str:
        return str(self.data.get("custom_caption") or "")

    def is_caption_disabled(self) -> bool:
        return bool(self.data.get("caption_disabled", False))

    def get_caption_template(self) -> str:
        if self.is_caption_disabled():
            return ""
        return self.get_caption() or DEFAULT_CAPTION_TEMPLATE

    def set_caption(self, template: str) -> None:
        self.data["custom_caption"] = str(template or "").strip()
        self.data["caption_disabled"] = False
        self._save()

    def clear_caption(self) -> None:
        self.data["custom_caption"] = ""
        self.data["caption_disabled"] = False
        self._save()

    def disable_caption(self) -> None:
        self.data["caption_disabled"] = True
        self._save()
