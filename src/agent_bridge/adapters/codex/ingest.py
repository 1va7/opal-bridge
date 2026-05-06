"""Codex rollout jsonl → canonical Session."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
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

from .apply_patch_parser import FileOp, parse_apply_patch, ApplyPatchParseError


logger = logging.getLogger(__name__)


def ingest(jsonl_path: Path | str, **opts: Any) -> Session:
    """Read a Codex rollout jsonl into a canonical Session."""
    p = Path(jsonl_path)
    raw_lines = _read_jsonl(p)
    if not raw_lines:
        raise ValueError(f"Empty or unreadable Codex session: {p}")

    sess_meta_line = raw_lines[0]
    if sess_meta_line.get("type") != "session_meta":
        # Codex requires session_meta as line 1 — but be lenient
        sess_meta_line = next(
            (l for l in raw_lines if l.get("type") == "session_meta"), {}
        )
    meta = sess_meta_line.get("payload", {}) if sess_meta_line else {}

    cwd = meta.get("cwd", str(p.parent))
    started_at = meta.get("timestamp") or iso_now_z()

    # turn_context lines: extract earliest one for permissions/model
    turn_ctx = next(
        (l for l in raw_lines if l.get("type") == "turn_context"), None
    )
    tc = turn_ctx.get("payload", {}) if turn_ctx else {}

    moments: list[Moment] = []
    for line in raw_lines:
        moments.extend(_translate_line(line, src_file=str(p), cwd=cwd))

    moments.sort(key=lambda m: m.ts)

    # If user named the thread in codex (`/name <foo>`), session_index.jsonl
    # holds the latest thread_name keyed by session id. Pull it so the CC
    # render can use it as title body instead of the first user prompt.
    thread_name = _read_codex_thread_name(meta.get("id"))

    return Session(
        id=uuid7_str(),
        source_harness="codex",
        source_session_id=meta.get("id", p.stem),
        source_session_path=str(p),
        cwd=cwd,
        git={
            "branch": (sess_meta_line.get("payload", {}).get("git") or {}).get("branch"),
            "commit": (sess_meta_line.get("payload", {}).get("git") or {}).get("commit_hash"),
            "repo_url": (sess_meta_line.get("payload", {}).get("git") or {}).get("repository_url"),
        },
        model_hint={
            "provider": "openai",
            "name": tc.get("model"),
            "reasoning_effort": tc.get("effort"),
        },
        started_at=started_at,
        ended_at=raw_lines[-1].get("timestamp") if raw_lines else None,
        permissions={
            "approval": tc.get("approval_policy"),
            "sandbox": (tc.get("sandbox_policy") or {}).get("type"),
        },
        moments=moments,
        source_metadata={
            "codex": {
                "originator": meta.get("originator"),
                "cli_version": meta.get("cli_version"),
                "source": meta.get("source"),
                "model_provider": meta.get("model_provider"),
                "personality": tc.get("personality"),
                "collaboration_mode": tc.get("collaboration_mode"),
                "thread_name": thread_name,
            }
        },
    )


def _read_codex_thread_name(session_id: str | None) -> str | None:
    """Reverse-scan ~/.codex/session_index.jsonl; return the latest **user-set**
    thread_name for the given id. Skips entries whose name was written by
    agent-bridge itself (matches `[from claude-code] ...` / `[from codex] ...`).

    Returns None if no user-set name exists.
    """
    if not session_id:
        return None
    from agent_bridge.adapters.codex.paths import CODEX_HOME
    idx_path = CODEX_HOME / "session_index.jsonl"
    if not idx_path.exists():
        return None
    user_set: str | None = None
    try:
        with idx_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("id") != session_id:
                    continue
                name = ev.get("thread_name", "") or ""
                # Skip our own auto-generated labels — they're not user choice
                if name.startswith("[from "):
                    continue
                user_set = name
    except OSError:
        pass
    return user_set


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


# Codex top-level types we don't translate to moments (metadata only)
_SKIP_TYPES = {"session_meta", "turn_context"}

# event_msg payload types that are duplicates of response_item — we drop them
_EVENT_MSG_DUPLICATES = {
    "user_message",       # mirror of response_item.message role=user
    "agent_message",      # mirror of response_item.message role=assistant
    "exec_command_end",   # mirror of function_call_output
    "patch_apply_end",    # mirror of custom_tool_call_output
    "web_search_end",     # mirror of web_search_call output
    "task_started", "task_complete", "turn_started", "turn_complete",
    "token_count",
    "thread_name_updated", "thread_rolled_back", "turn_aborted",
    "view_image_tool_call",
    "context_compacted",  # already covered by RolloutItem.compacted
}


def _translate_line(line: dict[str, Any], src_file: str, cwd: str) -> list[Moment]:
    line_type = line.get("type")
    if line_type in _SKIP_TYPES:
        return []
    ts = line.get("timestamp", iso_now_z())
    payload = line.get("payload", {}) or {}
    src_ref = {"file": src_file}

    if line_type == "compacted":
        return _translate_compacted(payload, ts, src_ref)

    if line_type == "event_msg":
        sub = payload.get("type")
        if sub in _EVENT_MSG_DUPLICATES:
            return []
        if sub == "error":
            return [Error(ts=ts, source_ref=src_ref, message=payload.get("message", ""))]
        return []  # unknown event_msg; drop

    if line_type == "response_item":
        return _translate_response_item(payload, ts, src_ref, cwd)

    return []


def _translate_response_item(payload: dict[str, Any], ts: str, src_ref: dict, cwd: str) -> list[Moment]:
    rt = payload.get("type")
    if rt == "message":
        return _translate_message(payload, ts, src_ref)
    if rt == "reasoning":
        return _translate_reasoning(payload, ts, src_ref)
    if rt == "function_call":
        return _translate_function_call(payload, ts, src_ref, cwd)
    if rt == "function_call_output":
        return [
            ToolResult(
                ts=ts,
                source_ref=src_ref,
                call_id=payload.get("call_id", ""),
                output_text=_normalize_output(payload.get("output", "")),
            )
        ]
    if rt == "custom_tool_call":
        return _translate_custom_tool_call(payload, ts, src_ref, cwd)
    if rt == "custom_tool_call_output":
        return [
            ToolResult(
                ts=ts,
                source_ref=src_ref,
                call_id=payload.get("call_id", ""),
                output_text=_normalize_output(payload.get("output", "")),
            )
        ]
    if rt == "web_search_call":
        action = payload.get("action", {}) or {}
        if action.get("type") == "search":
            return [
                ToolCall(
                    ts=ts, source_ref=src_ref,
                    tool="web_search", call_id=payload.get("call_id") or f"ws_{ts}",
                    args={"query": action.get("query", "")},
                    wire_native={"harness": "codex", "name": "web_search", "input": payload},
                )
            ]
        # open_page / find_in_page → web_fetch fallback
        return [
            ToolCall(
                ts=ts, source_ref=src_ref,
                tool="web_fetch", call_id=payload.get("call_id") or f"wf_{ts}",
                args={"url": action.get("url", "")},
                wire_native={"harness": "codex", "name": "web_search", "input": payload},
            )
        ]
    if rt == "image_generation_call":
        # No CC equivalent — emit a developer-style attachment for context
        return []
    if rt in {"compaction", "context_compaction", "compaction_summary"}:
        return [
            SummaryCompaction(
                ts=ts, source_ref=src_ref,
                summary_text="(codex remote compaction; encrypted_content not portable)",
            )
        ]
    return []


def _translate_message(payload: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    role = payload.get("role", "user")
    content = payload.get("content", []) or []
    text_parts: list[str] = []
    for c in content:
        ctype = c.get("type")
        if ctype in ("input_text", "output_text"):
            text_parts.append(c.get("text", ""))
        # input_image is not in MVP for ingest; future work
    text = "\n".join(text_parts)
    if role == "user":
        return [UserText(ts=ts, source_ref=src_ref, text=text)]
    if role == "assistant":
        phase = payload.get("phase")
        return [AssistantText(ts=ts, source_ref=src_ref, text=text, phase=phase)]
    if role == "developer":
        # treat as Attachment subtype="developer_note" (renderable as system-reminder)
        return [Attachment(ts=ts, source_ref=src_ref, subtype="developer_note", data={"text": text})]
    if role == "system":
        return [Attachment(ts=ts, source_ref=src_ref, subtype="system_note", data={"text": text})]
    return []


def _translate_reasoning(payload: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    summary = payload.get("summary") or []
    parts = [s.get("text", "") for s in summary if isinstance(s, dict)]
    text = "\n".join(p for p in parts if p)
    if not text:
        # Empty reasoning — drop silently
        return []
    return [Thinking(ts=ts, source_ref=src_ref, text=text, format="summary")]


def _translate_function_call(payload: dict[str, Any], ts: str, src_ref: dict, cwd: str) -> list[Moment]:
    name = payload.get("name", "")
    call_id = payload.get("call_id", "")
    raw_args = payload.get("arguments", "{}")
    namespace = payload.get("namespace")
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        args = {"_raw": raw_args}

    canonical, mapped_args = _codex_fn_to_canonical(name, args, cwd, namespace)
    return [
        ToolCall(
            ts=ts, source_ref=src_ref,
            tool=canonical, call_id=call_id,
            args=mapped_args,
            wire_native={"harness": "codex", "name": name, "namespace": namespace, "input": args},
        )
    ]


def _codex_fn_to_canonical(name: str, args: dict, cwd: str, namespace: str | None) -> tuple[str, dict]:
    if namespace:
        return "mcp_call", {"server": namespace, "tool": name, "args": args}
    if name in {"exec_command", "shell", "shell_command", "local_shell"}:
        cmd = args.get("cmd") or args.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        # Try to recognize the canonical tool from the command pattern
        canonical, mapped = _classify_shell_cmd(cmd, args, cwd)
        return canonical, mapped
    if name == "write_stdin":
        return "shell", {"command": f"echo {args.get('chars','')!r} | (existing pty stdin)", "_codex_pty": True}
    if name == "update_plan":
        plan = args.get("plan", [])
        return "update_plan", {"items": [
            {"title": p.get("step", ""), "status": p.get("status", "pending")} for p in plan
        ]}
    if name == "view_image":
        return "view_image", {"path": args.get("path", "")}
    if name in {"spawn_agent", "spawn_agent_v2"}:
        return "subagent_dispatch", {
            "agent_type": args.get("agent_type", "general-purpose"),
            "task": args.get("task_name", ""),
            "prompt": args.get("message", ""),
        }
    if name == "request_user_input":
        return "ask_user", args
    # Unknown — passthrough as shell echo for trace continuity
    return "shell", {"command": f"echo '(codex tool {name} args: {json.dumps(args, ensure_ascii=False)})'"}


_RG_FILES_RE = re.compile(r"^rg\s+--files(\s|$)")
_RG_RE = re.compile(r"^rg\s+")
_CAT_RE = re.compile(r"^cat\s+(-n\s+)?")
_HEAD_RE = re.compile(r"^head\s+-n\s+(\d+)\s+(.+)$")
_TAIL_RE = re.compile(r"^tail\s+-n\s+\+?(\d+)\s+(.+)$")
_SED_RE = re.compile(r"^sed\s+-n\s+'(\d+),(\d+)p'\s+(.+)$")


def _classify_shell_cmd(cmd: str, args: dict, cwd: str) -> tuple[str, dict]:
    """Recognize idiomatic shells we generate during CC→Codex (round-trip)."""
    cmd_s = cmd.strip()
    if _RG_FILES_RE.match(cmd_s):
        m = re.search(r"-g\s+(\S+)", cmd_s)
        return "find_files", {"pattern": _unquote(m.group(1)) if m else "*", "path": "."}
    if _RG_RE.match(cmd_s):
        # leave as full shell (Grep arg recovery is fragile); keep canonical=shell
        pass
    if _SED_RE.match(cmd_s):
        m = _SED_RE.match(cmd_s)
        a = int(m.group(1)); b = int(m.group(2)); path = _unquote(m.group(3))
        return "read_file", {"path": path, "offset": a, "limit": b - a + 1}
    if _HEAD_RE.match(cmd_s):
        m = _HEAD_RE.match(cmd_s)
        return "read_file", {"path": _unquote(m.group(2)), "limit": int(m.group(1))}
    if _TAIL_RE.match(cmd_s):
        m = _TAIL_RE.match(cmd_s)
        return "read_file", {"path": _unquote(m.group(2)), "offset": int(m.group(1))}
    if _CAT_RE.match(cmd_s):
        rest = cmd_s[len(re.match(_CAT_RE, cmd_s).group(0)):].strip()
        return "read_file", {"path": _unquote(rest)}
    return "shell", {
        "command": cmd,
        "workdir": args.get("workdir", cwd),
        **({"timeout_ms": args["yield_time_ms"]} if args.get("yield_time_ms") else {}),
    }


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _translate_custom_tool_call(payload: dict[str, Any], ts: str, src_ref: dict, cwd: str) -> list[Moment]:
    """custom_tool_call is almost always apply_patch in current Codex versions."""
    name = payload.get("name", "")
    call_id = payload.get("call_id", "")
    if name != "apply_patch":
        return [
            ToolCall(
                ts=ts, source_ref=src_ref,
                tool="shell", call_id=call_id,
                args={"command": f"echo '(unknown custom_tool_call {name})'"},
                wire_native={"harness": "codex", "name": name, "input": payload.get("input", "")},
            )
        ]

    patch_text = payload.get("input", "")
    try:
        ops = parse_apply_patch(patch_text)
    except ApplyPatchParseError as e:
        logger.warning("apply_patch parse failed: %s", e)
        return [
            ToolCall(
                ts=ts, source_ref=src_ref,
                tool="shell", call_id=call_id,
                args={"command": f"echo '(unparseable apply_patch: {e})'"},
                wire_native={"harness": "codex", "name": "apply_patch", "input": patch_text},
            )
        ]

    moments: list[Moment] = []
    for idx, op in enumerate(ops):
        # Each op gets a unique call_id derived from the parent call_id
        sub_id = call_id if len(ops) == 1 else f"{call_id}__{idx}"
        if op.kind == "add":
            moments.append(ToolCall(
                ts=ts, source_ref=src_ref, tool="write_file", call_id=sub_id,
                args={"path": _abspath(op.path, cwd), "content": "\n".join(op.add_lines)},
                wire_native={"harness": "codex", "name": "apply_patch", "input": patch_text},
            ))
        elif op.kind == "delete":
            moments.append(ToolCall(
                ts=ts, source_ref=src_ref, tool="delete_file", call_id=sub_id,
                args={"path": _abspath(op.path, cwd)},
                wire_native={"harness": "codex", "name": "apply_patch", "input": patch_text},
            ))
        elif op.kind == "update":
            if op.move_to and not op.hunks:
                moments.append(ToolCall(
                    ts=ts, source_ref=src_ref, tool="move_file", call_id=sub_id,
                    args={"from": _abspath(op.path, cwd), "to": _abspath(op.move_to, cwd)},
                    wire_native={"harness": "codex", "name": "apply_patch", "input": patch_text},
                ))
                continue
            # Convert each hunk into either a single Edit or a MultiEdit
            if len(op.hunks) == 1:
                h = op.hunks[0]
                moments.append(ToolCall(
                    ts=ts, source_ref=src_ref, tool="edit_file", call_id=sub_id,
                    args={
                        "path": _abspath(op.path, cwd),
                        "old": "\n".join(h.removed),
                        "new": "\n".join(h.added),
                    },
                    wire_native={"harness": "codex", "name": "apply_patch", "input": patch_text},
                ))
            else:
                moments.append(ToolCall(
                    ts=ts, source_ref=src_ref, tool="multi_edit_file", call_id=sub_id,
                    args={
                        "path": _abspath(op.path, cwd),
                        "edits": [{"old": "\n".join(h.removed), "new": "\n".join(h.added)} for h in op.hunks],
                    },
                    wire_native={"harness": "codex", "name": "apply_patch", "input": patch_text},
                ))
            if op.move_to:
                moments.append(ToolCall(
                    ts=ts, source_ref=src_ref, tool="move_file",
                    call_id=f"{sub_id}__mv",
                    args={"from": _abspath(op.path, cwd), "to": _abspath(op.move_to, cwd)},
                    wire_native={"harness": "codex", "name": "apply_patch", "input": patch_text},
                ))
    return moments


def _abspath(rel_or_abs: str, cwd: str) -> str:
    if rel_or_abs.startswith("/"):
        return rel_or_abs
    return f"{cwd.rstrip('/')}/{rel_or_abs}"


def _translate_compacted(payload: dict[str, Any], ts: str, src_ref: dict) -> list[Moment]:
    msg = payload.get("message", "")
    return [SummaryCompaction(ts=ts, source_ref=src_ref, summary_text=msg)]


def _normalize_output(out: Any) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        # FunctionCallOutputBody::ContentItems form
        parts = []
        for item in out:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return json.dumps(out, ensure_ascii=False)
