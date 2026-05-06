"""Canonical Session → Codex rollout jsonl.

MVP scope:
  - No subagent splitting (drops them with a warning).
  - apply_patch family is degraded to echo placeholder (defer to spec 002).
  - Generates session_meta + turn_context + response_items.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_bridge.canonical.ids import (
    add_ms,
    fmt_iso_z,
    iso_now_z,
    parse_cc_iso,
    uuid7_str,
)
from agent_bridge.canonical.schema import (
    AssistantText,
    Attachment,
    Error,
    ModeChange,
    Moment,
    Notification,
    PlanUpdate,
    Session,
    Thinking,
    ToolCall,
    ToolResult,
    UserText,
)

from .paths import CODEX_HOME, codex_path
from .tool_map import render_tool_call, render_tool_result


logger = logging.getLogger(__name__)


@dataclass
class RenderResult:
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
    model_name: str = "gpt-5.5",
    model_provider: str = "openai",
    timezone_name: str = "Asia/Shanghai",
) -> RenderResult:
    """Write canonical session as a Codex rollout jsonl."""
    home = Path(target_dir) if target_dir else CODEX_HOME

    uuid = uuid7_str()
    started_iso = session.started_at
    out_path = codex_path(uuid, started_iso, codex_home=home)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    if session.subagent_transcripts and subagent_strategy == "drop":
        warnings.append(
            f"Dropped {len(session.subagent_transcripts)} subagent transcript(s); use spec 002 for split/inline."
        )

    lines: list[dict[str, Any]] = []
    # 1. session_meta
    session_meta_payload: dict[str, Any] = {
        "id": uuid,
        "timestamp": started_iso,
        "cwd": session.cwd,
        "originator": "agent-bridge",
        "cli_version": "0.1.0",
        "source": {"custom": "agent-bridge"},
        "model_provider": model_provider,
    }
    lines.append({"timestamp": started_iso, "type": "session_meta", "payload": session_meta_payload})

    # 2. turn_context (1ms after session_meta)
    tc_ts = add_ms(started_iso, 1)
    turn_id = uuid7_str()
    current_date = parse_cc_iso(started_iso).strftime("%Y-%m-%d")
    permissions = session.permissions
    approval_policy = permissions.approval or "never"
    sandbox_type = permissions.sandbox or "danger-full-access"
    tc_payload: dict[str, Any] = {
        "turn_id": turn_id,
        "cwd": session.cwd,
        "current_date": current_date,
        "timezone": timezone_name,
        "approval_policy": approval_policy,
        "sandbox_policy": {"type": sandbox_type},
        "model": model_name,
        "summary": "none",
    }
    lines.append({"timestamp": tc_ts, "type": "turn_context", "payload": tc_payload})

    # 3+. response_items per moment
    for moment in session.moments:
        if moment.agent_scope.startswith("subagent:") and subagent_strategy == "drop":
            continue
        rendered = _render_moment(moment, session)
        lines.extend(rendered)

    _write_jsonl(out_path, lines)

    resume_command = f'codex exec resume {uuid} "<your prompt>" -o /tmp/agent-bridge-resume.md'

    return RenderResult(
        session_id=uuid,
        primary_path=out_path,
        sidecar_paths=[],
        resume_command=resume_command,
        warnings=warnings,
    )


def _render_moment(moment: Moment, session: Session) -> list[dict[str, Any]]:
    """Convert one canonical moment to 0+ Codex jsonl lines."""
    cwd = session.cwd
    ts = moment.ts

    if isinstance(moment, UserText):
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": moment.text}],
                },
            }
        ]

    if isinstance(moment, AssistantText):
        payload: dict[str, Any] = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": moment.text}],
        }
        if moment.phase:
            payload["phase"] = moment.phase
        return [{"timestamp": ts, "type": "response_item", "payload": payload}]

    if isinstance(moment, Thinking):
        # Already filtered in CC ingest, but defensive: drop.
        return []

    if isinstance(moment, ToolCall):
        payload = render_tool_call(moment.tool, moment.args, moment.call_id, cwd)
        return [{"timestamp": ts, "type": "response_item", "payload": payload}]

    if isinstance(moment, ToolResult):
        payload = render_tool_result(moment.call_id, moment.output_text, moment.is_error)
        return [{"timestamp": ts, "type": "response_item", "payload": payload}]

    if isinstance(moment, Attachment):
        text = _attachment_to_developer_text(moment)
        if not text:
            return []
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ]

    if isinstance(moment, ModeChange):
        # MVP: render as a developer note; turn_context-level switch is spec 003.
        text = f"(translated) Mode change: {moment.from_mode or '?'} → {moment.to_mode}"
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ]

    if isinstance(moment, PlanUpdate):
        # MVP: emit a plain update_plan function_call (output is suppressed by Codex anyway)
        plan_items = [
            {"step": it.get("title", ""), "status": it.get("status", "pending")}
            for it in moment.items
        ]
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "arguments": json.dumps({"plan": plan_items, "explanation": ""}, ensure_ascii=False),
                    "call_id": f"plan-{uuid7_str()[:8]}",
                },
            }
        ]

    if isinstance(moment, Error):
        text = f"(translated error) {moment.message}"
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ]

    if isinstance(moment, Notification):
        text = f"(notification:{moment.subtype}) {moment.content}"
        return [
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ]

    return []


def _attachment_to_developer_text(att: Attachment) -> str:
    """Format a CC attachment as a Codex developer message."""
    sub = att.subtype
    data = att.data

    if sub == "skill_listing":
        content = data.get("content", "")
        # Try to extract just skill names from the markdown bullet list
        names: list[str] = []
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("- ") and ":" in line:
                name = line[2:].split(":", 1)[0].strip()
                names.append(name)
        if names:
            bullet = "\n".join(f"- {n}" for n in names)
            return (
                "Available skills (translated from Claude Code skill_listing attachment):\n"
                + bullet
            )
        return "Available skills (translated from Claude Code skill_listing attachment):\n" + content[:2000]

    if sub == "nested_memory":
        inner = data.get("content", {})
        path = inner.get("path") or data.get("path", "?")
        content = inner.get("content", "") if isinstance(inner, dict) else str(inner)
        return f"Project memory ({path}):\n\n{content}"

    if sub == "task_reminder":
        items = data.get("content", [])
        if isinstance(items, list) and items:
            return "Active tasks:\n" + "\n".join(f"- {x}" for x in items)
        return "Active tasks: (none)"

    if sub == "file":
        filename = data.get("filename", "?")
        content = data.get("content", {})
        if isinstance(content, dict):
            file_content = content.get("file", {}).get("content", "")
        else:
            file_content = str(content)
        return f"User attached file `{filename}`:\n```\n{file_content[:8000]}\n```"

    if sub == "invoked_skills":
        skills = data.get("skills", [])
        if not skills:
            return ""
        out = ["Skill invoked:"]
        for s in skills:
            out.append(f"  • {s.get('name', '?')} (path: {s.get('path', '?')})")
        return "\n".join(out)

    if sub == "queued_command":
        return f"User queued: {data.get('prompt','')}"

    if sub == "date_change":
        return f"Date changed to {data.get('newDate','?')}"

    # Unknown subtype: dump raw
    return f"(attachment subtype={sub}): {json.dumps(data, ensure_ascii=False)[:500]}"


def _write_jsonl(path: Path, lines: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
