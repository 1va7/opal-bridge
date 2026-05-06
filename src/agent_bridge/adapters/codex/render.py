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
    SummaryCompaction,
    Thinking,
    ToolCall,
    ToolResult,
    UserText,
)

from .paths import CODEX_HOME, codex_path
from .apply_patch import build_file_state, FileStateCache
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
    model_provider: str | None = None,
    timezone_name: str = "Asia/Shanghai",
    session_id: str | None = None,
    title_prefix: str | None = None,  # ignored by codex render; here for CLI compat
    **_unused: Any,
) -> RenderResult:
    """Write canonical session as a Codex rollout jsonl.

    `session_id` (optional): override the generated UUIDv7. Pass a
    deterministic UUID (e.g., from `deterministic_uuid7`) to make sync
    idempotent — re-rendering with the same id overwrites the same file.
    """
    home = Path(target_dir) if target_dir else CODEX_HOME

    uuid = session_id or uuid7_str()
    started_iso = session.started_at
    out_path = codex_path(uuid, started_iso, codex_home=home)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    if session.subagent_transcripts and subagent_strategy == "drop":
        warnings.append(
            f"Dropped {len(session.subagent_transcripts)} subagent transcript(s); "
            f"use --subagent-strategy inline to splice them in."
        )
    if session.subagent_transcripts and subagent_strategy not in {"drop", "inline"}:
        warnings.append(f"Unknown subagent_strategy={subagent_strategy!r}; treating as 'drop'")
        subagent_strategy = "drop"

    lines: list[dict[str, Any]] = []
    # 1. session_meta
    session_meta_payload: dict[str, Any] = {
        "id": uuid,
        "timestamp": started_iso,
        "cwd": session.cwd,
        "originator": "agent-bridge",
        "cli_version": "0.1.0",
        # Use "cli" source so codex's resume picker shows us. Codex 0.128's
        # picker only includes sessions whose source ∈ INTERACTIVE_SESSION_SOURCES
        # (Cli, VsCode, Custom("atlas"), Custom("chatgpt")) — even with
        # --include-non-interactive. Custom("agent-bridge") is invisible.
        # The originator field above remains "agent-bridge" so the session is
        # still distinguishable from real CLI sessions on inspection.
        "source": "cli",
    }
    if model_provider:
        session_meta_payload["model_provider"] = model_provider
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

    # Pre-pass: build per-call file state snapshots for apply_patch context.
    file_snapshots = build_file_state(session.moments)

    # If inlining subagents, build a map from main_call_id → subagent_id by
    # matching dispatch ToolCall.args.task against subagent_meta.description.
    sub_inline_map: dict[str, str] = {}
    if subagent_strategy == "inline" and session.subagent_transcripts:
        sub_inline_map = _match_subagent_calls(session)
        used = set(sub_inline_map.values())
        for sa_id in session.subagent_transcripts:
            if sa_id not in used:
                warnings.append(
                    f"Subagent {sa_id} could not be matched to any dispatch call; "
                    "appending its transcript at the end of the main session."
                )

    # 3+. response_items per moment
    last_call_id_for_inline: str | None = None
    for moment in session.moments:
        if moment.agent_scope.startswith("subagent:") and subagent_strategy == "drop":
            continue
        rendered = _render_moment(moment, session, file_snapshots)
        lines.extend(rendered)
        # If this is a tool_result for a subagent dispatch, splice in the sub transcript right after
        if (
            subagent_strategy == "inline"
            and isinstance(moment, ToolResult)
            and moment.call_id in sub_inline_map
        ):
            sa_id = sub_inline_map[moment.call_id]
            sub_moments = session.subagent_transcripts.get(sa_id, [])
            lines.extend(_render_subagent_transcript(sa_id, sub_moments, session, file_snapshots))

    # Append unmatched subagent transcripts at the end
    if subagent_strategy == "inline" and session.subagent_transcripts:
        used = set(sub_inline_map.values())
        for sa_id, sub_moments in session.subagent_transcripts.items():
            if sa_id in used:
                continue
            lines.extend(_render_subagent_transcript(sa_id, sub_moments, session, file_snapshots))

    _write_jsonl(out_path, lines)
    _append_thread_name(home, uuid, _make_thread_name(session))
    _upsert_state_db(home, uuid, out_path, session, model_provider)

    resume_command = f'codex exec resume {uuid} "<your prompt>" -o /tmp/agent-bridge-resume.md'

    return RenderResult(
        session_id=uuid,
        primary_path=out_path,
        sidecar_paths=[],
        resume_command=resume_command,
        warnings=warnings,
    )


def _render_moment(
    moment: Moment,
    session: Session,
    file_snapshots: dict[str, FileStateCache] | None = None,
) -> list[dict[str, Any]]:
    """Convert one canonical moment to 0+ Codex jsonl lines."""
    cwd = session.cwd
    ts = moment.ts
    file_snapshots = file_snapshots or {}

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
        snap = file_snapshots.get(moment.call_id)
        payload = render_tool_call(moment.tool, moment.args, moment.call_id, cwd, file_state=snap)
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

    if isinstance(moment, SummaryCompaction):
        text = (
            f"(translated compact_boundary, trigger={moment.trigger}, "
            f"preTokens={moment.before_tokens}, postTokens={moment.after_tokens}) "
            "Prior conversation history was condensed; the next user message contains the summary."
        )
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


# Codex's harness wraps every session start with this synthetic envelope.
# Skip it when picking a title body so users see real prompts.
_CODEX_ENVELOPE_PREFIXES = (
    "<environment_context",
    "<permissions",
    "<collaboration_mode",
    "<skills_instructions",
    "<system-reminder",
    "<task-notification",
    "<local-command-stdout",
    "<local-command-caveat",
    "<command-name",
    "<command-message",
    "<command-args",
)


def _first_real_user_text(session: Session) -> str | None:
    """Return the first user_text moment that's not a harness envelope."""
    from agent_bridge.canonical.schema import UserText
    for m in session.moments:
        if not isinstance(m, UserText):
            continue
        text = m.text or ""
        stripped = text.lstrip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _CODEX_ENVELOPE_PREFIXES):
            continue
        return text
    return None


def _make_thread_name(session: Session) -> str:
    """Build a recognizable label for the resume picker's Conversation column."""
    body = _first_real_user_text(session) or session.source_session_id
    body = body.replace("\n", " ").replace("\r", " ").strip()
    if len(body) > 60:
        body = body[:57] + "..."
    return f"[from {session.source_harness}] {body}"


def _append_thread_name(codex_home: Path, thread_id: str, thread_name: str) -> None:
    """Append a SessionIndexEntry to ~/.codex/session_index.jsonl so the
    resume picker shows a recognizable label in the Conversation column.
    Append-only — last entry wins per Codex's reverse-scan logic.
    """
    from datetime import datetime, timezone
    idx_path = codex_home / "session_index.jsonl"
    entry = {
        "id": thread_id,
        "thread_name": thread_name,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with idx_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("failed to append %s: %s", idx_path, e)


def _upsert_state_db(
    codex_home: Path,
    thread_id: str,
    rollout_path: Path,
    session: Session,
    model_provider: str | None,
) -> None:
    """Insert/update a row into ~/.codex/state_5.sqlite threads table.

    Codex's resume picker pulls thread metadata from this DB. Files that
    only exist on disk (without a corresponding row) DO NOT show up in the
    picker, even though they're discoverable by UUID via `codex resume <uuid>`.

    We write minimum required fields with sensible defaults. Codex will
    reconcile the row on next access (filling in token counts, etc.).
    """
    import sqlite3
    from datetime import datetime, timezone
    db = codex_home / "state_5.sqlite"
    if not db.exists():
        return  # Codex hasn't initialized; let it create on next start

    title = _make_thread_name(session)
    first_msg = _first_real_user_text(session) or session.source_session_id
    if first_msg is None:
        first_msg = thread_id
    first_msg = first_msg.replace("\n", " ").strip()
    if len(first_msg) > 200:
        first_msg = first_msg[:200]

    started_at = session.started_at or datetime.now(timezone.utc).isoformat()
    try:
        from agent_bridge.canonical.ids import parse_cc_iso
        ts = int(parse_cc_iso(started_at).timestamp())
    except Exception:
        ts = int(datetime.now(timezone.utc).timestamp())
    ts_ms = ts * 1000

    # Use the user's configured provider so the picker's provider filter accepts us
    provider = model_provider or _read_user_default_provider(codex_home) or "openai"

    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            conn.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source,
                    model_provider, cwd, title, sandbox_policy, approval_mode,
                    tokens_used, has_user_event, archived,
                    cli_version, first_user_message, memory_mode,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    rollout_path=excluded.rollout_path,
                    updated_at=excluded.updated_at,
                    source=excluded.source,
                    model_provider=excluded.model_provider,
                    cwd=excluded.cwd,
                    title=excluded.title,
                    cli_version=excluded.cli_version,
                    first_user_message=excluded.first_user_message,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    thread_id,
                    str(rollout_path),
                    ts,
                    ts,
                    "cli",
                    provider,
                    session.cwd,
                    title,
                    '{"type":"danger-full-access"}',
                    "never",
                    0,
                    1,
                    0,
                    "0.1.0",
                    first_msg,
                    "enabled",
                    ts_ms,
                    ts_ms,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("state_db upsert failed for %s: %s", thread_id, e)


def _read_user_default_provider(codex_home: Path) -> str | None:
    """Read user's default model_provider from ~/.codex/config.toml."""
    cfg = codex_home / "config.toml"
    if not cfg.exists():
        return None
    try:
        import tomllib
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        return data.get("model_provider")
    except Exception:
        return None


def _match_subagent_calls(session: Session) -> dict[str, str]:
    """Match main session's subagent_dispatch ToolCalls to subagent_transcripts.

    Strategy: by description match against meta_dict, fallback chronological.
    Returns dict: main_call_id → agent_id
    """
    meta = session.source_metadata.get("claude_code", {}).get("subagent_meta", {}) or {}
    transcripts = session.subagent_transcripts or {}

    # description → list[agent_id]
    desc_to_ids: dict[str, list[str]] = {}
    for sa_id, m in meta.items():
        d = m.get("description") if isinstance(m, dict) else None
        if d:
            desc_to_ids.setdefault(d, []).append(sa_id)

    used: set[str] = set()
    result: dict[str, str] = {}

    # First pass: exact description match
    dispatches: list[ToolCall] = [
        m for m in session.moments
        if isinstance(m, ToolCall) and m.tool == "subagent_dispatch"
    ]
    dispatches.sort(key=lambda m: m.ts)

    for d in dispatches:
        wn = d.wire_native or {}
        desc = wn.get("input", {}).get("description") or d.args.get("task_description")
        if desc and desc in desc_to_ids:
            for sa_id in desc_to_ids[desc]:
                if sa_id not in used and sa_id in transcripts:
                    result[d.call_id] = sa_id
                    used.add(sa_id)
                    break

    # Second pass: leftover dispatches matched chronologically with leftover sub_ids
    leftover_dispatches = [d for d in dispatches if d.call_id not in result]
    leftover_subs = [
        (sa_id, sub_moments[0].ts if sub_moments else "")
        for sa_id, sub_moments in transcripts.items()
        if sa_id not in used
    ]
    leftover_subs.sort(key=lambda t: t[1])

    for d, (sa_id, _) in zip(leftover_dispatches, leftover_subs):
        result[d.call_id] = sa_id
        used.add(sa_id)

    return result


def _render_subagent_transcript(
    sa_id: str,
    sub_moments: list[Moment],
    session: Session,
    file_snapshots: dict[str, FileStateCache],
) -> list[dict[str, Any]]:
    """Render a sub transcript inline as developer-bracketed messages."""
    if not sub_moments:
        return []
    out: list[dict[str, Any]] = []
    boundary_ts_start = sub_moments[0].ts
    boundary_ts_end = sub_moments[-1].ts
    out.append({
        "timestamp": boundary_ts_start,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{
                "type": "input_text",
                "text": f"--- begin subagent transcript ({sa_id}) ---",
            }],
        },
    })
    for m in sub_moments:
        out.extend(_render_moment(m, session, file_snapshots))
    out.append({
        "timestamp": boundary_ts_end,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{
                "type": "input_text",
                "text": f"--- end subagent transcript ({sa_id}) ---",
            }],
        },
    })
    return out
