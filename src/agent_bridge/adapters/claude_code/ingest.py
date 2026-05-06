"""Claude Code session jsonl → canonical Session.

MVP scope: single-file (no DAG merging), drop subagents, drop compact_boundary
(raise NotImplementedError).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent_bridge.canonical.ids import iso_now_z, uuid7_str
from agent_bridge.canonical.schema import (
    AssistantText,
    Attachment,
    Error,
    ModeChange,
    Moment,
    Notification,
    PlanUpdate,
    Session,
    SummaryCompaction,
    Thinking,
    ToolCall,
    ToolResult,
    UserText,
)

from .tool_map import cc_tool_to_canonical, translate_args


logger = logging.getLogger(__name__)


# Top-level CC line types we always drop in MVP
DROPPED_TYPES = {
    "permission-mode",
    "file-history-snapshot",
    "last-prompt",
    "ai-title",
    "custom-title",
    "agent-name",
    "queue-operation",
}

# system subtypes dropped
DROPPED_SYSTEM_SUBTYPES = {
    "turn_duration",
    "stop_hook_summary",
    "away_summary",
    "api_error",  # converted to Error elsewhere if we want; for MVP drop
}


def ingest(jsonl_path: Path | str, **opts: Any) -> Session:
    """Read a CC session jsonl into a canonical Session."""
    p = Path(jsonl_path)
    raw_lines = _read_jsonl(p)
    if not raw_lines:
        raise ValueError(f"Empty or unreadable CC session: {p}")

    header_info = _extract_header(raw_lines)
    moments: list[Moment] = []
    for line in raw_lines:
        m_list = _translate_line(line, src_file=str(p))
        moments.extend(m_list)

    # Sort by timestamp string (ISO 8601 sorts lexicographically)
    moments.sort(key=lambda m: m.ts)
    _post_infer_assistant_phase(moments)

    return Session(
        id=uuid7_str(),
        source_harness="claude-code",
        source_session_id=header_info["session_id"],
        source_session_path=str(p),
        cwd=header_info["cwd"],
        git={"branch": header_info.get("git_branch"), "commit": None, "repo_url": None},
        model_hint={
            "provider": "anthropic",
            "name": header_info.get("model"),
            "reasoning_effort": None,
        },
        started_at=header_info["started_at"],
        ended_at=header_info["ended_at"],
        moments=moments,
        source_metadata={
            "claude_code": {
                "version": header_info.get("version"),
                "userType": header_info.get("user_type"),
                "entrypoint": header_info.get("entrypoint"),
            }
        },
    )


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSONL line: %r", raw[:80])
    return out


def _post_infer_assistant_phase(moments: list[Moment]) -> None:
    """Walk moments left-to-right; for each AssistantText, set phase based on
    whether any ToolCall appears between this moment and the next UserText.
    """
    n = len(moments)
    for i, m in enumerate(moments):
        if not isinstance(m, AssistantText):
            continue
        # Find next UserText (turn boundary)
        end = n
        for j in range(i + 1, n):
            if isinstance(moments[j], UserText):
                end = j
                break
        # Look for ToolCall between i+1 and end
        has_tool_after = any(
            isinstance(moments[k], ToolCall) for k in range(i + 1, end)
        )
        m.phase = "commentary" if has_tool_after else "final_answer"


def _extract_header(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull session-level metadata from the first lines that have it."""
    header: dict[str, Any] = {}
    for line in lines:
        if "sessionId" in line and "session_id" not in header:
            header["session_id"] = line["sessionId"]
        if "cwd" in line and "cwd" not in header:
            header["cwd"] = line["cwd"]
        if "gitBranch" in line and "git_branch" not in header:
            header["git_branch"] = line["gitBranch"]
        if "version" in line and "version" not in header:
            header["version"] = line["version"]
        if "userType" in line and "user_type" not in header:
            header["user_type"] = line["userType"]
        if "entrypoint" in line and "entrypoint" not in header:
            header["entrypoint"] = line["entrypoint"]
        msg = line.get("message")
        if isinstance(msg, dict) and "model" in msg and "model" not in header:
            header["model"] = msg["model"]

    timestamps = [l["timestamp"] for l in lines if "timestamp" in l]
    header["started_at"] = min(timestamps) if timestamps else iso_now_z()
    header["ended_at"] = max(timestamps) if timestamps else None
    return header


def _translate_line(line: dict[str, Any], src_file: str) -> list[Moment]:
    """Dispatch by type. Returns 0+ moments."""
    line_type = line.get("type")
    if line_type in DROPPED_TYPES:
        return []
    if line_type == "system":
        if line.get("subtype") in DROPPED_SYSTEM_SUBTYPES:
            return []
        if line.get("subtype") == "compact_boundary":
            return _translate_compact_boundary(line, ts, src_ref)
        return []

    src_ref = {"file": src_file, "uuid": line.get("uuid")}
    ts = line.get("timestamp", iso_now_z())

    if line_type == "user":
        return _translate_user(line, ts, src_ref)
    if line_type == "assistant":
        return _translate_assistant(line, ts, src_ref)
    if line_type == "attachment":
        return _translate_attachment(line, ts, src_ref)
    # Unknown line type: emit a metadata moment (not lost)
    logger.info("Skipping unrecognized CC line type: %s", line_type)
    return []


def _translate_user(line: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    msg = line.get("message", {})
    content = msg.get("content")
    prompt_id = line.get("promptId")
    sidechain = line.get("isSidechain", False)
    scope = "main"
    if sidechain:
        scope = f"subagent:{line.get('agentId', 'unknown')}"

    # Compact summary user (synthetic): becomes a UserText preserving the summary text
    if line.get("isCompactSummary"):
        text = content if isinstance(content, str) else json.dumps(content)
        return [
            UserText(
                ts=ts,
                source_ref=src_ref,
                agent_scope=scope,
                text=text,
                prompt_id=prompt_id,
                lossy=True,
                lossy_reason="synthetic isCompactSummary user; original pre-compact history not preserved",
            )
        ]

    if isinstance(content, str):
        return [
            UserText(
                ts=ts,
                source_ref=src_ref,
                agent_scope=scope,
                text=content,
                prompt_id=prompt_id,
            )
        ]

    if isinstance(content, list):
        out: list[Moment] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                out.append(
                    UserText(
                        ts=ts,
                        source_ref=src_ref,
                        agent_scope=scope,
                        text=block.get("text", ""),
                        prompt_id=prompt_id,
                    )
                )
            elif btype == "tool_result":
                content_val = block.get("content", "")
                if isinstance(content_val, list):
                    text_parts = [
                        b.get("text", "") for b in content_val if b.get("type") == "text"
                    ]
                    output_text = "\n".join(text_parts)
                    output_blocks = [b for b in content_val if b.get("type") != "text"]
                else:
                    output_text = str(content_val)
                    output_blocks = []
                out.append(
                    ToolResult(
                        ts=ts,
                        source_ref=src_ref,
                        agent_scope=scope,
                        call_id=block.get("tool_use_id", ""),
                        output_text=output_text,
                        output_blocks=output_blocks,
                        is_error=block.get("is_error", False),
                    )
                )
        return out

    return []


def _translate_assistant(line: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    msg = line.get("message", {})
    content = msg.get("content", [])
    sidechain = line.get("isSidechain", False)
    scope = "main"
    if sidechain:
        scope = f"subagent:{line.get('agentId', 'unknown')}"

    if not isinstance(content, list):
        return []

    # First, count if there are any tool_use blocks (drives phase inference for text blocks)
    has_tool_use = any(b.get("type") == "tool_use" for b in content)

    out: list[Moment] = []
    seen_text_after_tool = False
    seen_tool = False
    for block in content:
        btype = block.get("type")
        if btype == "thinking":
            # MVP: drop entirely (drop signature + text both — see tool-mapping.md §0)
            continue
        if btype == "redacted_thinking":
            continue
        if btype == "text":
            text_val = block.get("text", "")
            # phase inference: commentary if there's a tool_use later in this block list, else final_answer
            phase: str
            if not has_tool_use:
                phase = "final_answer"
            elif not seen_tool:
                phase = "commentary"
            else:
                phase = "final_answer"
                seen_text_after_tool = True
            out.append(
                AssistantText(
                    ts=ts,
                    source_ref=src_ref,
                    agent_scope=scope,
                    text=text_val,
                    phase=phase,
                )
            )
        elif btype == "tool_use":
            seen_tool = True
            cc_name = block.get("name", "")
            cc_input = block.get("input", {})
            canonical = cc_tool_to_canonical(cc_name)
            args = translate_args(canonical, cc_input)
            out.append(
                ToolCall(
                    ts=ts,
                    source_ref=src_ref,
                    agent_scope=scope,
                    tool=canonical,
                    call_id=block.get("id", ""),
                    args=args,
                    wire_native={
                        "harness": "claude-code",
                        "name": cc_name,
                        "input": cc_input,
                    },
                )
            )
    return out


def _translate_attachment(line: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    att = line.get("attachment", {})
    subtype = att.get("type", "unknown")
    # Pass through subtype-specific fields as data
    data = {k: v for k, v in att.items() if k != "type"}
    return [
        Attachment(
            ts=ts,
            source_ref=src_ref,
            subtype=subtype,
            data=data,
        )
    ]


def _translate_compact_boundary(line: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    """A compact_boundary line marks where prior history was condensed.

    The summary text itself lives in the next CC line (a synthetic user with
    isCompactSummary=true). Here we just emit a SummaryCompaction marker
    carrying the metadata; the summary text becomes a regular UserText via
    _translate_user.
    """
    meta = line.get("compactMetadata", {}) or {}
    return [
        SummaryCompaction(
            ts=ts,
            source_ref=src_ref,
            trigger=meta.get("trigger", "auto"),
            before_tokens=meta.get("preTokens"),
            after_tokens=meta.get("postTokens"),
        )
    ]
