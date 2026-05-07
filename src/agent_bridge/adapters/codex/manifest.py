"""Track which Codex jsonls agent-bridge wrote, to distinguish them from
files that have been extended or renamed by the user via `codex resume`.

Used by `_list_codex_sessions` to skip agent-bridge-written files that
the user hasn't touched, preventing the 'echo' explosion where every CC
source produced both a codex translation AND a CC mirror of that
codex translation.

Two signals together imply 'user-touched, must translate back to CC':
  - file size differs from what we wrote (user appended turns), OR
  - user has set a thread_name in ~/.codex/session_index.jsonl (user
    renamed the session via codex's /name command)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


MANIFEST_PATH = Path.home() / ".cache" / "agent-bridge" / "codex-write-manifest.json"


def _read() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    try:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("failed to write %s: %s", MANIFEST_PATH, e)


def record_write(rollout_path: Path, codex_id: str) -> None:
    """Record that we just wrote this file. Stores size + mtime."""
    try:
        st = rollout_path.stat()
    except OSError:
        return
    data = _read()
    data[codex_id] = {
        "path": str(rollout_path),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }
    _write(data)


def get(codex_id: str) -> dict | None:
    return _read().get(codex_id)


def clear() -> None:
    if MANIFEST_PATH.exists():
        try:
            MANIFEST_PATH.unlink()
        except OSError:
            pass
