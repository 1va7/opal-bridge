"""End-to-end test: translate the PoC fixture and verify structure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.translator import translate


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "data" / "fixture" / "cc-input.jsonl"
HAND_TRANSLATED = REPO_ROOT / "data" / "fixture" / "codex-output.jsonl"


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_translate_fixture_structure(tmp_path: Path) -> None:
    """Translator output must structurally match the hand-crafted PoC reference."""
    res = translate(
        source_path=FIXTURE,
        source_harness="claude-code",
        target_harness="codex",
        target_dir=tmp_path,
    )
    assert res.primary_path.exists()
    out_lines = _read_jsonl(res.primary_path)
    ref_lines = _read_jsonl(HAND_TRANSLATED)

    # Same line count
    assert len(out_lines) == len(ref_lines), (
        f"line count mismatch: got {len(out_lines)}, expected {len(ref_lines)}"
    )

    # Same top-level type sequence
    out_types = [l["type"] for l in out_lines]
    ref_types = [l["type"] for l in ref_lines]
    assert out_types == ref_types

    # Same payload types
    out_pls = [l.get("payload", {}).get("type") for l in out_lines]
    ref_pls = [l.get("payload", {}).get("type") for l in ref_lines]
    assert out_pls == ref_pls

    # First line must be session_meta with our originator
    assert out_lines[0]["type"] == "session_meta"
    assert out_lines[0]["payload"]["originator"] == "agent-bridge"
    assert out_lines[0]["payload"]["source"] == {"custom": "agent-bridge"}

    # Second line must be turn_context referencing same cwd
    assert out_lines[1]["type"] == "turn_context"
    assert out_lines[1]["payload"]["cwd"] == out_lines[0]["payload"]["cwd"]

    # tool_use ↔ tool_result call_id pairing preserved
    pairs: dict[str, dict] = {}
    for line in out_lines:
        pl = line.get("payload", {})
        if pl.get("type") == "function_call":
            pairs.setdefault(pl["call_id"], {})["call"] = pl
        elif pl.get("type") == "function_call_output":
            pairs.setdefault(pl["call_id"], {})["output"] = pl
    for cid, pair in pairs.items():
        assert "call" in pair and "output" in pair, f"missing pairing for {cid}"

    # Phase inference: assistant before tool_call should be commentary
    assistant_msgs = [
        (i, l)
        for i, l in enumerate(out_lines)
        if l["payload"].get("type") == "message" and l["payload"].get("role") == "assistant"
    ]
    # The first assistant message is mid-turn (tool call follows)
    assert assistant_msgs[0][1]["payload"]["phase"] == "commentary"
    # The last is final
    assert assistant_msgs[-1][1]["payload"]["phase"] == "final_answer"


def test_translate_session_id_is_uuidv7(tmp_path: Path) -> None:
    """Codex requires UUIDv7 for session id; verify version nibble."""
    res = translate(
        source_path=FIXTURE,
        source_harness="claude-code",
        target_harness="codex",
        target_dir=tmp_path,
    )
    # UUIDv7 has 7 in the 13th hex digit (position 14 incl. dashes: aaaaaaaa-bbbb-7ccc-...)
    parts = res.session_id.split("-")
    assert parts[2].startswith("7"), f"not UUIDv7: {res.session_id}"


def test_unsupported_direction_raises(tmp_path: Path) -> None:
    """Hermes adapter not implemented yet; pair should still raise."""
    with pytest.raises(NotImplementedError):
        translate(
            source_path=FIXTURE,
            source_harness="hermes",
            target_harness="codex",
            target_dir=tmp_path,
        )
