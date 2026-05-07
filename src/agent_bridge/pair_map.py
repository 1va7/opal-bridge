"""Track CC↔Codex twin pairs so renames propagate as title updates
to the existing twin instead of creating a duplicate file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


PAIR_MAP_PATH = Path.home() / ".cache" / "agent-bridge" / "pair-map.json"


def _read() -> dict:
    if not PAIR_MAP_PATH.exists():
        return {"cc_to_codex": {}, "codex_to_cc": {}}
    try:
        d = json.loads(PAIR_MAP_PATH.read_text(encoding="utf-8"))
        d.setdefault("cc_to_codex", {})
        d.setdefault("codex_to_cc", {})
        return d
    except (json.JSONDecodeError, OSError):
        return {"cc_to_codex": {}, "codex_to_cc": {}}


def _write(data: dict) -> None:
    try:
        PAIR_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        PAIR_MAP_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("failed to write %s: %s", PAIR_MAP_PATH, e)


def record(*, cc_id: str, codex_id: str) -> None:
    """Record a twin pair. Called by render after writing one direction."""
    d = _read()
    d["cc_to_codex"][cc_id] = codex_id
    d["codex_to_cc"][codex_id] = cc_id
    _write(d)


def get_cc_for_codex(codex_id: str) -> str | None:
    return _read()["codex_to_cc"].get(codex_id)


def get_codex_for_cc(cc_id: str) -> str | None:
    return _read()["cc_to_codex"].get(cc_id)


def clear() -> None:
    if PAIR_MAP_PATH.exists():
        try:
            PAIR_MAP_PATH.unlink()
        except OSError:
            pass


def bootstrap_from_disk(cc_root: Path, codex_root: Path) -> int:
    """Scan existing files to populate the pair map retroactively.

    For each REAL CC session (no `[from ...]` custom-title), compute
    deterministic_uuid7("cc:<cc_id>", started_at) for plausible started_at
    timestamps from its first line; then for each candidate, check if a
    matching codex jsonl exists. If yes, record the pair.

    We can't reverse hash codex_id → cc_id directly, so we go cc → codex.
    """
    from agent_bridge.canonical.ids import deterministic_uuid7
    pairs = 0
    d = _read()

    # Index existing codex files by id for fast lookup
    codex_id_to_path: dict[str, Path] = {}
    if codex_root.exists():
        for f in codex_root.glob("**/rollout-*.jsonl"):
            # Filename: rollout-YYYY-MM-DDTHH-MM-SS-<UUID>.jsonl
            stem = f.stem
            parts = stem.split("-")
            if len(parts) >= 6:
                cand = "-".join(parts[-5:])
                if len(cand) == 36 and cand.count("-") == 4:
                    codex_id_to_path[cand] = f

    if cc_root.exists():
        for proj in cc_root.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                # Skip CC files we generated as translations of codex
                try:
                    head = f.open(encoding="utf-8", errors="replace").read(4096)
                except OSError:
                    continue
                is_translation = False
                cc_started_at = None
                for line in head.splitlines()[:5]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "custom-title" and isinstance(ev.get("customTitle"), str) and ev["customTitle"].startswith("[from "):
                        is_translation = True
                        break
                    if cc_started_at is None and ev.get("timestamp"):
                        cc_started_at = ev["timestamp"]
                if is_translation:
                    continue
                if not cc_started_at:
                    continue
                cc_id = f.stem
                expected_codex_id = deterministic_uuid7(f"cc:{cc_id}", cc_started_at)
                if expected_codex_id in codex_id_to_path:
                    d["cc_to_codex"][cc_id] = expected_codex_id
                    d["codex_to_cc"][expected_codex_id] = cc_id
                    pairs += 1

    _write(d)
    return pairs
