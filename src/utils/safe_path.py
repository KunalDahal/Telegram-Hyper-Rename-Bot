"""Filename/path sanitization helpers.

Several places in this codebase build a filesystem path out of a filename
that ultimately comes from an untrusted source (a Telegram-supplied
`file_name` attribute, or a filename typed by a user into a rename
command). If that string is joined into a directory without being
sanitized first, it can contain `..` segments or an absolute path and
escape the intended directory - letting a caller make the bot read from or
write to an arbitrary path on disk.

Use `safe_filename()` wherever an incoming name is first accepted, and
`safe_join()` at the point a name is actually joined into a directory, so
the guarantee holds even if some future code path forgets to sanitize
up front.
"""

from __future__ import annotations

import os
import re

_UNSAFE_CHARS = re.compile(r"[\\/\x00-\x1f]")


def safe_filename(name: str, fallback: str = "file") -> str:
    """Reduce `name` to a single, safe filesystem path component.

    - Strips any directory components (so `../../etc/passwd` becomes
      `passwd`, and an absolute path collapses to its basename).
    - Removes path separators and control characters that could remain.
    - Rejects the empty string and anything that is only dots (`.`, `..`),
      falling back to `fallback` instead.
    """
    if not name:
        return fallback

    # Drop any directory components first. os.path.basename handles both
    # `../`-style traversal and absolute paths on the current platform.
    candidate = os.path.basename(name.strip())

    # Belt-and-braces: also strip separators/control chars that
    # os.path.basename wouldn't catch (e.g. a literal backslash on a
    # POSIX host, which basename() there treats as a normal character).
    candidate = _UNSAFE_CHARS.sub("_", candidate)

    # A name made up purely of dots (".", "..", "...") refers to the
    # current/parent directory rather than a real file - never allow it
    # to survive as-is.
    candidate = candidate.strip()
    if candidate.strip(".") == "":
        return fallback

    return candidate or fallback


def safe_join(base_dir: str, name: str, fallback: str = "file") -> str:
    """Join `name` under `base_dir`, guaranteeing the result stays inside it.

    This is the last line of defense at the point of use: even if `name`
    somehow reaches here unsanitized, the resulting path is verified to
    resolve inside `base_dir` before being returned.
    """
    base_dir = os.path.abspath(base_dir)
    candidate = safe_filename(name, fallback=fallback)
    final_path = os.path.abspath(os.path.join(base_dir, candidate))

    if final_path != base_dir and not final_path.startswith(base_dir + os.sep):
        # Should be unreachable given safe_filename() above, but never
        # trust a single layer of defense for a path-traversal guard.
        final_path = os.path.join(base_dir, fallback)

    return final_path
