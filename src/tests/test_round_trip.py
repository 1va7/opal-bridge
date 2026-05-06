"""Round-trip tests across CC ↔ Codex translation."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from agent_bridge.adapters import claude_code, codex
from agent_bridge.canonical.schema import (
    AssistantText,
    ToolCall,
    ToolResult,
    UserText,
)
from agent_bridge.translator import translate


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "data" / "fixture" / "cc-input.jsonl"


def _kind_counts(moments) -> Counter:
    return Counter(type(m).__name__ for m in moments)


def test_cc_to_codex_to_cc_preserves_essentials(tmp_path: Path) -> None:
    """Translate CC → Codex (jsonl in tmp), then Codex → CC, verify counts."""
    # 1. CC → Codex
    res1 = translate(
        source_path=FIXTURE,
        source_harness="claude-code",
        target_harness="codex",
        target_dir=tmp_path / "codex",
    )
    codex_path = res1.primary_path
    assert codex_path.exists()

    # 2. Codex → canonical (so we can assert on moments)
    canonical1 = codex.ingest(codex_path)

    # 3. Codex → CC
    res2 = translate(
        source_path=codex_path,
        source_harness="codex",
        target_harness="claude-code",
        target_dir=tmp_path / "claude",
    )
    cc_path = res2.primary_path
    assert cc_path.exists()

    # 4. Re-ingest the CC output
    canonical2 = claude_code.ingest(cc_path, follow_subagents=False)

    # User text should round-trip 1:1
    user_in = [m for m in canonical1.moments if isinstance(m, UserText)]
    user_out = [m for m in canonical2.moments if isinstance(m, UserText)]
    assert len(user_out) >= len(user_in), (
        f"Lost user_text moments through round-trip: {len(user_in)} → {len(user_out)}"
    )

    # Assistant text count should be similar (allow ±1 for phase rendering quirks)
    asst_in = [m for m in canonical1.moments if isinstance(m, AssistantText)]
    asst_out = [m for m in canonical2.moments if isinstance(m, AssistantText)]
    assert abs(len(asst_in) - len(asst_out)) <= 1

    # Tool call count must match exactly
    tc_in = [m for m in canonical1.moments if isinstance(m, ToolCall)]
    tc_out = [m for m in canonical2.moments if isinstance(m, ToolCall)]
    assert len(tc_in) == len(tc_out), (
        f"Tool call count drift: codex side={len(tc_in)}, cc-rt side={len(tc_out)}"
    )


def test_codex_ingest_real_session_no_crash(tmp_path: Path) -> None:
    """Smoke test: synthesize a small Codex session and ingest it."""
    sess = {"timestamp": "2026-05-06T10:00:00.000Z", "type": "session_meta",
            "payload": {"id": "019dfd00-0000-7000-9000-000000000001",
                        "timestamp": "2026-05-06T10:00:00.000Z",
                        "cwd": "/tmp/x", "originator": "test", "cli_version": "0.1.0"}}
    tc = {"timestamp": "2026-05-06T10:00:00.001Z", "type": "turn_context",
          "payload": {"turn_id": "019dfd00-0000-7000-9000-000000000002",
                      "cwd": "/tmp/x", "approval_policy": "never",
                      "sandbox_policy": {"type": "danger-full-access"},
                      "model": "gpt-5.5", "summary": "none"}}
    user = {"timestamp": "2026-05-06T10:00:01.000Z", "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}]}}
    fc = {"timestamp": "2026-05-06T10:00:02.000Z", "type": "response_item",
          "payload": {"type": "function_call", "name": "exec_command",
                      "arguments": json.dumps({"cmd": "pwd", "workdir": "/tmp/x"}),
                      "call_id": "call_x"}}
    fco = {"timestamp": "2026-05-06T10:00:03.000Z", "type": "response_item",
           "payload": {"type": "function_call_output", "call_id": "call_x",
                       "output": "/tmp/x"}}
    asst = {"timestamp": "2026-05-06T10:00:04.000Z", "type": "response_item",
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                        "phase": "final_answer"}}
    p = tmp_path / "codex.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [sess, tc, user, fc, fco, asst]) + "\n")

    s = codex.ingest(p)
    assert s.source_harness == "codex"
    assert s.cwd == "/tmp/x"
    assert s.source_session_id == "019dfd00-0000-7000-9000-000000000001"
    counts = _kind_counts(s.moments)
    assert counts.get("UserText", 0) == 1
    assert counts.get("AssistantText", 0) == 1
    assert counts.get("ToolCall", 0) == 1
    assert counts.get("ToolResult", 0) == 1


def test_apply_patch_parser_split_into_canonical_calls(tmp_path: Path) -> None:
    """custom_tool_call apply_patch with multiple ops → multiple canonical ToolCalls."""
    sess = {"timestamp": "2026-05-06T10:00:00.000Z", "type": "session_meta",
            "payload": {"id": "019dfd00-0000-7000-9000-000000000003",
                        "timestamp": "2026-05-06T10:00:00.000Z",
                        "cwd": "/tmp/x", "originator": "t", "cli_version": "0.1.0"}}
    patch_text = (
        "*** Begin Patch\n"
        "*** Add File: hello.py\n"
        "+import os\n"
        "+print(os.getcwd())\n"
        "*** Update File: app.py\n"
        "@@\n"
        "-old_line\n"
        "+new_line\n"
        "*** Delete File: stale.py\n"
        "*** End Patch\n"
    )
    ctc = {"timestamp": "2026-05-06T10:00:01.000Z", "type": "response_item",
           "payload": {"type": "custom_tool_call", "name": "apply_patch",
                       "call_id": "call_p", "input": patch_text, "status": "completed"}}
    ctco = {"timestamp": "2026-05-06T10:00:02.000Z", "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "call_p",
                        "output": json.dumps({"output": "ok", "metadata": {"exit_code": 0}})}}
    p = tmp_path / "codex.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in [sess, ctc, ctco]) + "\n")

    s = codex.ingest(p)
    tc_moments = [m for m in s.moments if isinstance(m, ToolCall)]
    # 3 ops → 3 ToolCalls
    assert len(tc_moments) == 3
    tools = sorted(m.tool for m in tc_moments)
    assert tools == ["delete_file", "edit_file", "write_file"]
