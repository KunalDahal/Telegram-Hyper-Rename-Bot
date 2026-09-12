import logging
import os
from pyrogram.file_id import FileId

logger = logging.getLogger(__name__)

def _parse_allowed_dcs() -> frozenset[int]:
    raw = os.getenv("DC", "").strip()
    if not raw:
        return frozenset()    
    allowed: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit():
            dc = int(token)
            if 1 <= dc <= 5:
                allowed.add(dc)
    return frozenset(allowed)

ALLOWED_DCS: frozenset[int] = _parse_allowed_dcs()


# ── DC extraction ─────────────────────────────────────────────────────────────

def get_file_dc(file_id: str) -> int | None:
    try:
        decoded = FileId.decode(file_id)
        dc_id   = decoded.dc_id
        if dc_id and 1 <= dc_id <= 5:
            logger.debug("[DCChecker] file_id %s… → DC%d", file_id[:20], dc_id)
            return dc_id
        return None
    except Exception:
        return None


# ── Public gate ───────────────────────────────────────────────────────────────

def is_dc_allowed(file_id: str) -> bool:
    if not ALLOWED_DCS:
        return True

    dc = get_file_dc(file_id)
    if dc is None:
        return True

    return dc in ALLOWED_DCS