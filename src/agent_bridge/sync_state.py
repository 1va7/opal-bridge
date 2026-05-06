"""Source-side sync state — tracks size+mtime of files we've translated.

Lets sync skip unchanged sources without relying on target file mtime,
so target files can have their mtime backdated to the original session
timestamp (for sane picker sort order) without breaking idempotency.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


STATE_PATH = Path.home() / ".cache" / "agent-bridge" / "sync-state.json"


def _read() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("failed to write %s: %s", STATE_PATH, e)


def is_unchanged(source: Path, direction_key: str, fingerprint: str = "") -> bool:
    """Return True iff source has same size+mtime+fingerprint as last successful translate.

    fingerprint: an extra string to detect changes that don't show up in
    size/mtime (e.g. codex thread_name updates via `/name`).
    """
    key = f"{direction_key}|{source}"
    rec = _read().get(key)
    if not rec:
        return False
    try:
        st = source.stat()
    except OSError:
        return False
    return (
        st.st_size == rec.get("size")
        and abs(st.st_mtime - float(rec.get("mtime", 0))) < 1.0
        and rec.get("fingerprint", "") == fingerprint
    )


def mark_translated(source: Path, direction_key: str, fingerprint: str = "") -> None:
    key = f"{direction_key}|{source}"
    try:
        st = source.stat()
    except OSError:
        return
    data = _read()
    data[key] = {"size": st.st_size, "mtime": st.st_mtime, "fingerprint": fingerprint}
    _write(data)


def clear() -> None:
    """Wipe all state — used by `clean` so a fresh sync re-translates everything."""
    if STATE_PATH.exists():
        try:
            STATE_PATH.unlink()
        except OSError:
            pass
