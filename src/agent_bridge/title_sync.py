"""Title propagation between CC↔Codex twin sessions.

When the user renames a session via codex's `/name` command, push the
new name into the original CC twin's `custom-title` row (instead of
creating a duplicate CC file). And vice versa: when the user changes
the CC custom title, push it into codex's session_index.jsonl.

This lets both sides converge on a single shared name without
duplicating the conversation file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from agent_bridge import pair_map
from agent_bridge.adapters.claude_code.render import CC_PROJECTS
from agent_bridge.adapters.codex.paths import CODEX_HOME
from agent_bridge.adapters.codex.ingest import _read_codex_thread_name


logger = logging.getLogger(__name__)


def propagate_titles(log: callable = print) -> int:
    """Walk known twin pairs; if user renamed one side, append the new
    title to the other. Returns count of propagated renames.
    """
    propagated = 0

    # Build lookup of CC files by sessionId
    cc_files: dict[str, Path] = {}
    if CC_PROJECTS.exists():
        for proj in CC_PROJECTS.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                cc_files[f.stem] = f

    pair_data = pair_map._read()
    for codex_id, cc_id in pair_data.get("codex_to_cc", {}).items():
        cc_path = cc_files.get(cc_id)
        if cc_path is None:
            continue

        codex_user_name = _read_codex_thread_name(codex_id)
        cc_latest_title = _read_latest_cc_custom_title(cc_path)

        # Skip if neither side has a meaningful user-set title
        if not codex_user_name and not (cc_latest_title and not cc_latest_title.startswith("[from ")):
            continue

        # Determine canonical name: prefer the more recent rename
        codex_ts = _latest_codex_thread_name_ts(codex_id)
        cc_ts = _latest_cc_custom_title_ts(cc_path) if cc_latest_title else None

        canonical = None
        if codex_user_name and (not cc_latest_title or codex_ts and cc_ts and codex_ts > cc_ts):
            canonical = codex_user_name
        elif cc_latest_title and not cc_latest_title.startswith("[from "):
            canonical = cc_latest_title

        if not canonical:
            continue

        # Push into CC if its latest title differs
        if cc_latest_title != canonical:
            _append_cc_custom_title(cc_path, cc_id, canonical)
            log(f"  ⇄ rename → CC {cc_id[:12]}  '{canonical[:60]}'")
            propagated += 1

        # Push into codex session_index if its latest user name differs
        if codex_user_name != canonical:
            _append_codex_thread_name(codex_id, canonical)
            log(f"  ⇄ rename → codex {codex_id[:14]}  '{canonical[:60]}'")
            propagated += 1

    return propagated


# ---------- helpers ---------- #


def _read_latest_cc_custom_title(path: Path) -> str | None:
    latest = None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "custom-title":
                    latest = ev.get("customTitle")
    except OSError:
        pass
    return latest


def _latest_cc_custom_title_ts(path: Path) -> float | None:
    """Approx ts of latest custom-title row (uses file mtime as fallback)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _latest_codex_thread_name_ts(codex_id: str) -> float | None:
    idx = CODEX_HOME / "session_index.jsonl"
    if not idx.exists():
        return None
    latest = None
    try:
        with idx.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("id") != codex_id:
                    continue
                ts = ev.get("updated_at", "")
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    latest = dt.timestamp()
                except (ValueError, AttributeError):
                    pass
    except OSError:
        pass
    return latest


def _append_cc_custom_title(path: Path, sess_id: str, title: str) -> None:
    """Append a custom-title row to the CC jsonl. CC's picker uses the
    latest custom-title row per session (per our harness research)."""
    entry = {"type": "custom-title", "customTitle": title, "sessionId": sess_id}
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("failed to append custom-title to %s: %s", path, e)


def _append_codex_thread_name(codex_id: str, name: str) -> None:
    idx = CODEX_HOME / "session_index.jsonl"
    entry = {
        "id": codex_id,
        "thread_name": name,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    idx.parent.mkdir(parents=True, exist_ok=True)
    try:
        with idx.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("failed to append %s: %s", idx, e)
