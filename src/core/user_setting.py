"""Per-user rename preferences backed by MongoDB, with legacy JSON migration."""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from copy import deepcopy
from typing import Any, Dict

from .user_settings_store import UserSettingsStore, UserSettingsStoreError


logger = logging.getLogger(__name__)


DEFAULT_WATERMARK = {
    "enabled": False,
    "text": "",
    "color": "white",
    "font_path": "",
    "font_name": "default",
    "font_size": 24,
    "padding": 7,
    "timing_mode": "range",
    "start": 0,
    "end": 0,
    "duration": 30,
    "repeat_count": 1,
    "position": "bot_right",
}

VALID_WM_POSITIONS = {
    "top_left", "top_mid", "top_right",
    "mid_left", "mid_right",
    "bot_left", "bot_right",
}

VALID_WM_TIMING_MODES = {"full", "range", "random_duration"}
VALID_SEND_TYPES = {"media", "document"}

DEFAULT_METADATA = {
    "movie_name": "",
    "artist": "",
    "author": "",
    "title_all": "",
    "encoder": "",
}


def _extract_font_name(font_path: str) -> str:
    try:
        from fontTools.ttLib import TTFont

        tt = TTFont(font_path, fontNumber=0)
        name_table = tt["name"]
        for name_id in (4, 1):
            record = name_table.getName(name_id, 3, 1, 0x0409)
            if record:
                return record.toUnicode().strip()
        for record in name_table.names:
            if record.nameID == 4:
                try:
                    return record.toUnicode().strip()
                except Exception:
                    pass
    except Exception:
        pass
    return os.path.splitext(os.path.basename(font_path))[0]


class UserSettings:
    """Mutable settings object that persists updates to MongoDB immediately."""

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
        self._load()

    @property
    def temp_state(self) -> Dict[int, Dict]:
        """Short-lived chat interaction state; intentionally not persisted."""
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

        watermark = self.data.get("watermark")
        if not isinstance(watermark, dict):
            watermark = {}
        for key, value in DEFAULT_WATERMARK.items():
            watermark.setdefault(key, value)
        watermark["enabled"] = self._as_bool(watermark["enabled"])
        watermark["text"] = str(watermark["text"] or "")
        watermark["color"] = (
            str(watermark["color"]).lower()
            if str(watermark["color"]).lower() in {"white", "black"}
            else "white"
        )
        watermark["timing_mode"] = (
            str(watermark["timing_mode"])
            if str(watermark["timing_mode"]) in VALID_WM_TIMING_MODES
            else DEFAULT_WATERMARK["timing_mode"]
        )
        watermark["position"] = (
            str(watermark["position"])
            if str(watermark["position"]) in VALID_WM_POSITIONS
            else DEFAULT_WATERMARK["position"]
        )
        for key, minimum, maximum in (
            ("font_size", 8, 96),
            ("padding", 0, 30),
            ("start", 0, 86_400),
            ("end", 0, 86_400),
            ("duration", 1, 3_600),
            ("repeat_count", 1, 20),
        ):
            watermark[key] = self._clamp_int(watermark[key], minimum, maximum)
        self.data["watermark"] = watermark

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
        """Keep valid zero padding while preventing batch-renaming crashes."""
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

        watermark = self.data["watermark"]
        font_id = str(watermark.get("font_asset_id") or "")
        if font_id:
            suffix = os.path.splitext(str(watermark.get("font_asset_name") or "font.ttf"))[1] or ".ttf"
            font_path = os.path.join(self.fonts_folder, f"font_{self.user_id}{suffix}")
            if self.store.restore_asset(font_id, font_path):
                watermark["font_path"] = os.path.abspath(font_path)
            else:
                watermark["font_path"] = ""

    def _migrate_legacy_assets(self) -> None:
        """Copy pre-Mongo thumbnail/font files into GridFS on first access."""
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

        watermark = self.data["watermark"]
        font_path = str(watermark.get("font_path") or "")
        if font_path and os.path.isfile(font_path) and not watermark.get("font_asset_id"):
            try:
                watermark["font_asset_id"] = self.store.upload_asset(
                    self.user_id, "watermark_font", font_path
                )
                watermark["font_asset_name"] = os.path.basename(font_path)
            except UserSettingsStoreError:
                logger.exception("Could not migrate watermark font for user %s", self.user_id)

    def _save_legacy_file(self) -> None:
        try:
            with open(self.storage_path, "w", encoding="utf-8") as target:
                json.dump(self.data, target, indent=2)
        except Exception:
            logger.exception("Could not write local settings fallback for user %s", self.user_id)

    def _save(self) -> None:
        if self.store:
            try:
                self.store.save(self.user_id, self.data)
                return
            except UserSettingsStoreError:
                logger.exception("Could not save settings for user %s to MongoDB", self.user_id)
        self._save_legacy_file()

    def _get_default_settings(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "send_type": "media",
            "auto_detect_thumb": False,
            "metadata": deepcopy(DEFAULT_METADATA),
            "thumbnail_path": "",
            "thumbnail_asset_id": "",
            "watermark": deepcopy(DEFAULT_WATERMARK),
            "format": "{title} S{season}E{episode} [{quality}] [{audio}].mkv",
            "default_start_episode": 1,
            "default_season": 1,
            "default_audio": "SUB",
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

    def get_watermark(self) -> Dict[str, Any]:
        """Return a detached snapshot for menus and queued tasks."""
        self._normalize()
        return deepcopy(self.data["watermark"])

    def update_watermark(self, **kwargs) -> None:
        self._normalize()
        watermark = self.data["watermark"]
        for key, value in kwargs.items():
            if key in DEFAULT_WATERMARK:
                watermark[key] = value
        self._normalize()
        self._save()

    def set_watermark_font(self, tmp_path: str) -> str:
        if not tmp_path or not os.path.exists(tmp_path):
            return "default"
        extension = os.path.splitext(tmp_path)[1].lower()
        if extension not in (".ttf", ".otf"):
            return "default"

        font_filename = f"font_{self.user_id}_{uuid.uuid4().hex[:8]}{extension}"
        destination = os.path.abspath(os.path.join(self.fonts_folder, font_filename))
        shutil.copy2(tmp_path, destination)
        self._normalize()
        watermark = self.data["watermark"]
        previous_asset_id = str(watermark.get("font_asset_id") or "")
        if self.store:
            try:
                watermark["font_asset_id"] = self.store.upload_asset(
                    self.user_id, "watermark_font", destination, previous_asset_id
                )
                watermark["font_asset_name"] = font_filename
            except UserSettingsStoreError:
                logger.exception("Could not persist watermark font for user %s", self.user_id)

        old_path = str(watermark.get("font_path") or "")
        fonts_root = os.path.abspath(self.fonts_folder)
        if old_path and old_path != destination and os.path.exists(old_path) and os.path.abspath(old_path).startswith(fonts_root):
            try:
                os.remove(old_path)
            except OSError:
                pass

        font_name = _extract_font_name(destination)
        watermark["font_path"] = destination
        watermark["font_name"] = font_name
        self._save()
        return font_name

    def reset_watermark(self) -> None:
        watermark = self.get_watermark()
        self._remove_local_file(str(watermark.get("font_path") or ""), self.fonts_folder)
        if self.store:
            self.store.delete_asset(str(watermark.get("font_asset_id") or ""))
        self.data["watermark"] = deepcopy(DEFAULT_WATERMARK)
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
        if self.store:
            try:
                self.data["thumbnail_asset_id"] = self.store.upload_asset(
                    self.user_id, "thumbnail", destination, previous_asset_id
                )
            except UserSettingsStoreError:
                logger.exception("Could not persist thumbnail for user %s", self.user_id)
        self.data["thumbnail_path"] = destination
        self._save()

    def clear_thumbnail(self) -> None:
        self._remove_local_file(str(self.data.get("thumbnail_path") or ""), self.thumbnails_folder)
        if self.store:
            self.store.delete_asset(str(self.data.get("thumbnail_asset_id") or ""))
        self.data["thumbnail_asset_id"] = ""
        self.data["thumbnail_path"] = ""
        self._save()

    def _delete_assets(self) -> None:
        self.clear_thumbnail()
        self.reset_watermark()

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
