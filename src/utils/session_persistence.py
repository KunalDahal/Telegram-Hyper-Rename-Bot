
from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Union

from pyrogram.storage.sqlite_storage import SQLiteStorage

logger = logging.getLogger(__name__)


def fingerprint(session_string: str) -> str:
    return hashlib.sha256(session_string.encode("utf-8")).hexdigest()[:16]


_fingerprint = fingerprint


def _is_valid_session_file(path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute("SELECT number FROM version").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


async def ensure_persistent_session(
    name: str,
    workdir: Union[str, Path],
    session_string: Optional[str],
) -> None:
    session_string = (session_string or "").strip()
    if not session_string:
        return

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    session_path = workdir / f"{name}.session"
    fingerprint_path = workdir / f"{name}.session.source"

    new_fingerprint = _fingerprint(session_string)
    existing_fingerprint = (
        fingerprint_path.read_text().strip() if fingerprint_path.exists() else None
    )

    if session_path.exists() and existing_fingerprint == new_fingerprint:
        if _is_valid_session_file(session_path):
            logger.debug(
                "[Session] Persistent session for %r already matches the "
                "configured SESSION_STRING; keeping its peer cache.",
                name,
            )
            return
        logger.warning(
            "[Session] Persistent session for %r matches the configured "
            "SESSION_STRING's fingerprint but %s has no usable 'version' "
            "table (a corrupt/partial file from a previous run). "
            "Rebuilding it instead of trusting the stale fingerprint.",
            name, session_path,
        )
    elif existing_fingerprint is not None and existing_fingerprint != new_fingerprint:
        logger.warning(
            "[Session] SESSION_STRING for %r changed since the last run; "
            "rebuilding %s (previous peer cache is discarded).",
            name, session_path,
        )
    else:
        logger.info(
            "[Session] No persistent session file yet for %r; "
            "materializing one at %s from SESSION_STRING.",
            name, session_path,
        )

    mem_storage = SQLiteStorage(
        name, workdir=workdir, session_string=session_string, in_memory=True
    )
    await mem_storage.open()
    try:
        await mem_storage.auth_key()

        if session_path.exists():
            session_path.unlink()

        await mem_storage.conn.commit()

        file_conn = sqlite3.connect(str(session_path))
        try:
            await mem_storage.conn.backup(file_conn)
            try:
                version_row = file_conn.execute(
                    "SELECT number FROM version"
                ).fetchone()
            except sqlite3.OperationalError as verify_err:
                raise RuntimeError(
                    f"Backup of session {name!r} to {session_path} did not "
                    f"produce a valid Pyrogram session file: {verify_err}"
                ) from verify_err
            if version_row is None:
                raise RuntimeError(
                    f"Backup of session {name!r} to {session_path} produced "
                    "a version table with no row."
                )
        finally:
            file_conn.close()
    finally:
        await mem_storage.close()

    fingerprint_path.write_text(new_fingerprint)
    logger.info("[Session] Persistent session ready for %r at %s", name, session_path)