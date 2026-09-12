"""Make SESSION_STRING-based Pyrogram clients keep a durable, on-disk peer
cache across container restarts.

Why this exists
----------------
Both `Client("...", session_string=X, in_memory=False)` calls in this
codebase used to *look* like they'd get file-backed storage once
`in_memory` was False. They didn't. Pyrogram/kurigram's own
`Client.__init__` picks the storage engine like this (confirmed against the
installed `kurigram` build)::

    if self.session_string:
        self.storage = SQLiteStorage(name, workdir=workdir, session_string=session_string, in_memory=True)
    elif self.in_memory:
        self.storage = SQLiteStorage(name, workdir=workdir, in_memory=True)
    ...
    else:
        self.storage = SQLiteStorage(name, workdir=workdir)

Passing `session_string` *at all* forces `in_memory=True`, full stop --
the `in_memory` constructor argument is only consulted when
`session_string` is falsy. So a client built with
`session_string=config.session_string, in_memory=False` silently got
`MemoryStorage` anyway, and its peer cache (the access-hash table
`resolve_peer` depends on) was wiped on every restart. That's the actual
cause of the "works right after a resolve, breaks again after a restart"
`CHANNEL_INVALID` / `ID not found` pattern.

Pyrogram also has no built-in way to seed a *file-backed* session from a
session string -- `SQLiteStorage.open()` only decodes `session_string`
when `in_memory=True`; the file-storage branch ignores it entirely and
just creates an empty database. So a one-time migration step is required.

What this does
---------------
`ensure_persistent_session()` materializes `{workdir}/{name}.session` from
a SESSION_STRING using a live in-memory SQLiteStorage (opened the normal
way, so decoding stays correct across pyrogram/kurigram versions instead
of hand-parsing the session-string struct format), then does a raw SQLite
`backup()` of that populated database into the on-disk file.

After that, the *real* Client should be constructed with
`workdir=..., in_memory=False` and no `session_string` at all, so it opens
the file directly. Every peer it resolves during that run is then written
straight to disk, and survives the next restart.

Re-running with the same SESSION_STRING is a cheap no-op (a fingerprint of
the string is stored alongside the session file). If the configured
SESSION_STRING changes -- e.g. the operator rotates to a different
Premium account -- the stale file is rebuilt instead of silently reused,
since a changed credential should not be treated as unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Union

from pyrogram.storage.sqlite_storage import SQLiteStorage

logger = logging.getLogger(__name__)


def fingerprint(session_string: str) -> str:
    """Short, stable fingerprint of a SESSION_STRING.

    Exposed so callers can cheaply check "is this the same credential I
    already have a client running for?" without touching disk -- useful to
    guard against building a second Client on top of the same on-disk
    session file (see Worker._premium_session_fingerprint).
    """
    return hashlib.sha256(session_string.encode("utf-8")).hexdigest()[:16]


# Backwards-compatible private alias used within this module.
_fingerprint = fingerprint


async def ensure_persistent_session(
    name: str,
    workdir: Union[str, Path],
    session_string: Optional[str],
) -> None:
    """Ensure `{workdir}/{name}.session` exists and matches `session_string`.

    No-op if `session_string` is empty (interactive/file-only login is left
    exactly as Pyrogram normally handles it) or if a session file already
    on disk was built from this exact SESSION_STRING.
    """
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
        logger.debug(
            "[Session] Persistent session for %r already matches the "
            "configured SESSION_STRING; keeping its peer cache.",
            name,
        )
        return

    if existing_fingerprint is not None and existing_fingerprint != new_fingerprint:
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
        # Decoding a bad/expired SESSION_STRING should fail loudly here,
        # the same way it would have failed inside client.start() before --
        # not be swallowed into a silent fallback.
        await mem_storage.auth_key()

        if session_path.exists():
            session_path.unlink()

        # Flush pending writes on the source connection first: sqlite's
        # backup() step can otherwise deadlock against an open write
        # transaction on the very same connection.
        mem_storage.conn.commit()

        file_conn = sqlite3.connect(str(session_path))
        try:
            mem_storage.conn.backup(file_conn)
        finally:
            file_conn.close()
    finally:
        await mem_storage.close()

    fingerprint_path.write_text(new_fingerprint)
    logger.info("[Session] Persistent session ready for %r at %s", name, session_path)
