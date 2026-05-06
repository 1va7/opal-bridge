"""Minimal canonical → CC jsonl render. PoC stage — only handles the
moment kinds needed to validate that `claude --resume` accepts our output.
Full feature set lands in 003d.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_bridge.canonical.ids import iso_now_z
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


logger = logging.getLogger(__name__)


CC_PROJECTS = Path.home() / ".claude" / "projects"


def encode_cwd(cwd: str) -> str:
    """CC's encoded-cwd directory naming.

    CC calls realpath() then NFC normalize before encoding. We do the same
    so that `claude --resume` from the same cwd finds our file.

    Replace every non-[a-zA-Z0-9] char with '-'. Truncate to 200 chars and
    append a base36 stable hash if longer.
    See docs/claude-code-harness.md §1.2.
    """
    import os
    import unicodedata
    try:
        real = os.path.realpath(cwd)
    except OSError:
        real = cwd
    real = unicodedata.normalize("NFC", real)
    s = re.sub(r"[^a-zA-Z0-9]", "-", real)
    if len(s) <= 200:
        return s
    h = 0
    for c in real:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return s[:200] + "-" + format(h, "x")


def new_uuid_v4() -> str:
    return str(uuid.uuid4())


@dataclass
class CcRenderResult:
    session_id: str
    primary_path: Path
    sidecar_paths: list[Path] = field(default_factory=list)
    resume_command: str = ""
    warnings: list[str] = field(default_factory=list)


def render(
    session: Session,
    *,
    target_dir: Path | str | None = None,
    fidelity: str = "A",
    subagent_strategy: str = "drop",
    cc_version: str = "2.1.131",
    **_unused: Any,
) -> CcRenderResult:
    """Canonical → CC jsonl, one file under ~/.claude/projects/<encoded>/."""
    home_projects = Path(target_dir) if target_dir else CC_PROJECTS
    encoded = encode_cwd(session.cwd)
    proj_dir = home_projects / encoded
    proj_dir.mkdir(parents=True, exist_ok=True)

    sess_id = new_uuid_v4()
    out_path = proj_dir / f"{sess_id}.jsonl"

    warnings: list[str] = []
    if session.subagent_transcripts and subagent_strategy != "inline":
        warnings.append(
            f"Dropped {len(session.subagent_transcripts)} subagent transcript(s) for CC render; "
            f"--subagent-strategy=inline not yet supported in 003a."
        )

    lines: list[dict[str, Any]] = []
    parent: str | None = None

    for moment in session.moments:
        cc_lines = _render_moment(moment, session, sess_id, cc_version, parent_uuid=parent)
        for ln in cc_lines:
            lines.append(ln)
            parent = ln["uuid"]

    _write_jsonl(out_path, lines)

    resume_cmd = f'claude --resume {sess_id}'

    return CcRenderResult(
        session_id=sess_id,
        primary_path=out_path,
        sidecar_paths=[],
        resume_command=resume_cmd,
        warnings=warnings,
    )


def _render_moment(
    moment: Moment,
    session: Session,
    sess_id: str,
    cc_version: str,
    *,
    parent_uuid: str | None,
) -> list[dict[str, Any]]:
    base = {
        "sessionId": sess_id,
        "cwd": session.cwd,
        "version": cc_version,
        "userType": "external",
        "entrypoint": "cli",
        "gitBranch": session.git.branch or "HEAD",
        "isSidechain": False,
    }
    if isinstance(moment, UserText):
        u = new_uuid_v4()
        line = {
            **base,
            "uuid": u,
            "parentUuid": parent_uuid,
            "type": "user",
            "message": {"role": "user", "content": moment.text},
            "timestamp": moment.ts,
        }
        if moment.prompt_id:
            line["promptId"] = moment.prompt_id
        return [line]

    if isinstance(moment, AssistantText):
        u = new_uuid_v4()
        # Generate a fake msg_id; CC accepts opaque strings here
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        return [{
            **base,
            "uuid": u,
            "parentUuid": parent_uuid,
            "type": "assistant",
            "timestamp": moment.ts,
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": session.model_hint.name or "claude-opus-4-6",
                "content": [{"type": "text", "text": moment.text}],
                "stop_reason": "end_turn" if moment.phase != "commentary" else "tool_use",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }]

    if isinstance(moment, Thinking):
        # Drop entirely — no portable signature.
        return []

    if isinstance(moment, ToolCall):
        u = new_uuid_v4()
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        cc_name, cc_input = _canonical_to_cc_tool(moment)
        return [{
            **base,
            "uuid": u,
            "parentUuid": parent_uuid,
            "type": "assistant",
            "timestamp": moment.ts,
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": session.model_hint.name or "claude-opus-4-6",
                "content": [{
                    "type": "tool_use",
                    "id": moment.call_id,
                    "name": cc_name,
                    "input": cc_input,
                }],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }]

    if isinstance(moment, ToolResult):
        u = new_uuid_v4()
        return [{
            **base,
            "uuid": u,
            "parentUuid": parent_uuid,
            "type": "user",
            "timestamp": moment.ts,
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": moment.call_id,
                    "content": moment.output_text,
                    "is_error": moment.is_error,
                }],
            },
        }]

    if isinstance(moment, Attachment):
        u = new_uuid_v4()
        # CC attachment line shape
        return [{
            **base,
            "uuid": u,
            "parentUuid": parent_uuid,
            "type": "attachment",
            "timestamp": moment.ts,
            "attachment": {"type": moment.subtype, **moment.data},
        }]

    if isinstance(moment, SummaryCompaction):
        # CC needs TWO lines: compact_boundary + synthetic isCompactSummary user
        boundary_uuid = new_uuid_v4()
        sum_uuid = new_uuid_v4()
        return [
            {
                **base,
                "uuid": boundary_uuid,
                "parentUuid": None,
                "logicalParentUuid": parent_uuid,
                "type": "system",
                "subtype": "compact_boundary",
                "content": "Conversation compacted",
                "isMeta": False,
                "level": "info",
                "timestamp": moment.ts,
                "compactMetadata": {
                    "trigger": moment.trigger,
                    "preTokens": moment.before_tokens or 0,
                    "postTokens": moment.after_tokens or 0,
                    "durationMs": 0,
                },
            },
            {
                **base,
                "uuid": sum_uuid,
                "parentUuid": boundary_uuid,
                "type": "user",
                "isCompactSummary": True,
                "isVisibleInTranscriptOnly": True,
                "timestamp": moment.ts,
                "message": {"role": "user", "content": moment.summary_text},
            },
        ]

    if isinstance(moment, ModeChange):
        return [{
            "type": "permission-mode",
            "permissionMode": _mode_to_cc(moment.to_mode),
            "sessionId": sess_id,
        }]

    if isinstance(moment, Error):
        return []

    if isinstance(moment, PlanUpdate):
        # Render as a TaskCreate-style fake; later 003d may improve
        u = new_uuid_v4()
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        first = moment.items[0] if moment.items else {"title": "(empty plan)", "status": "pending"}
        return [{
            **base,
            "uuid": u,
            "parentUuid": parent_uuid,
            "type": "assistant",
            "timestamp": moment.ts,
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": session.model_hint.name or "claude-opus-4-6",
                "content": [{
                    "type": "tool_use",
                    "id": f"toolu_plan_{uuid.uuid4().hex[:16]}",
                    "name": "TaskCreate",
                    "input": {"subject": first.get("title", ""), "description": ""},
                }],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        }]

    if isinstance(moment, Notification):
        return []

    return []


def _canonical_to_cc_tool(call: ToolCall) -> tuple[str, dict[str, Any]]:
    """Map canonical tool name + args back to CC wire name + input.

    Falls back to wire_native for round-trip fidelity when available.
    """
    if call.wire_native and call.wire_native.get("harness") == "claude-code":
        return call.wire_native.get("name", "Bash"), call.wire_native.get("input", {})

    args = dict(call.args)
    canonical = call.tool

    if canonical == "shell":
        return "Bash", {
            "command": args.get("command", ""),
            "description": args.get("description") or "",
            **({"timeout": args["timeout_ms"]} if args.get("timeout_ms") else {}),
            **({"run_in_background": True} if args.get("run_in_background") else {}),
        }
    if canonical == "read_file":
        return "Read", {
            "file_path": args.get("path", ""),
            **({"offset": args["offset"]} if args.get("offset") else {}),
            **({"limit": args["limit"]} if args.get("limit") else {}),
        }
    if canonical == "write_file":
        return "Write", {"file_path": args.get("path", ""), "content": args.get("content", "")}
    if canonical == "edit_file":
        return "Edit", {
            "file_path": args.get("path", ""),
            "old_string": args.get("old", ""),
            "new_string": args.get("new", ""),
            **({"replace_all": True} if args.get("replace_all") else {}),
        }
    if canonical == "multi_edit_file":
        return "MultiEdit", {"file_path": args.get("path", ""), "edits": args.get("edits", [])}
    if canonical == "find_files":
        return "Glob", {"pattern": args.get("pattern", ""), **({"path": args["path"]} if args.get("path") else {})}
    if canonical == "search_text":
        return "Grep", args
    if canonical == "web_fetch":
        return "WebFetch", {"url": args.get("url", ""), "prompt": args.get("prompt", "")}
    if canonical == "web_search":
        return "WebSearch", {"query": args.get("query", "")}
    if canonical == "subagent_dispatch":
        return "Agent", {
            "description": args.get("task", ""),
            "prompt": args.get("prompt", ""),
            "subagent_type": args.get("agent_type", "general-purpose"),
        }
    if canonical == "ask_user":
        return "AskUserQuestion", args
    if canonical == "view_image":
        return "Read", {"file_path": args.get("path", "")}
    if canonical == "mcp_call":
        srv = args.get("server", "?")
        tool = args.get("tool", "?")
        return f"mcp__{srv}__{tool}", args.get("args", {})
    # Fallback
    return "Bash", {"command": f"echo '(translated unknown tool: {canonical})'"}


def _mode_to_cc(canonical_mode: str) -> str:
    return {
        "default": "default",
        "plan": "plan",
        "never": "bypassPermissions",
        "on-request": "default",
    }.get(canonical_mode, "default")


def _write_jsonl(path: Path, lines: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
