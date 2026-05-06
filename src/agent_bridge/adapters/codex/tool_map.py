"""Canonical tool → Codex wire format translation.

See docs/tool-mapping.md §3.
"""

from __future__ import annotations

import json
import shlex
from typing import Any


def render_tool_call(tool: str, args: dict[str, Any], call_id: str, cwd: str) -> dict[str, Any]:
    """
    Return the Codex `response_item.payload` dict for a canonical tool call.
    Most map to function_call (exec_command); apply_patch family go to custom_tool_call.
    """
    if tool == "shell":
        cmd = args.get("command", "")
        wd = args.get("workdir", cwd)
        codex_args = {"cmd": cmd, "workdir": wd, "yield_time_ms": 1000}
        # max_output_tokens omitted unless specified — Codex default kicks in
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps(codex_args, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "read_file":
        path = args.get("path", "")
        offset = args.get("offset")
        limit = args.get("limit")
        if offset and limit:
            cmd = f"sed -n '{offset},{offset + limit - 1}p' {shlex.quote(path)}"
        elif offset:
            cmd = f"tail -n +{offset} {shlex.quote(path)}"
        elif limit:
            cmd = f"head -n {limit} {shlex.quote(path)}"
        else:
            cmd = f"cat -n {shlex.quote(path)}"
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": cwd, "yield_time_ms": 1000}, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "find_files":
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        cmd = f"rg --files -g {shlex.quote(pattern)} {shlex.quote(path)}"
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": cwd, "yield_time_ms": 5000}, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "search_text":
        cmd = _build_rg_command(args)
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": cwd, "yield_time_ms": 10000}, ensure_ascii=False),
            "call_id": call_id,
        }
    if tool == "web_search":
        # Codex web_search is a Responses-builtin; we approximate by exec_command curl in MVP
        # because we can't actually invoke OpenAI's builtin from a translated session.
        # The historical record just preserves the call as a comment.
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps(
                {
                    "cmd": f"echo '(translated from CC WebSearch query: {args.get('query','')})'",
                    "workdir": cwd,
                    "yield_time_ms": 1000,
                },
                ensure_ascii=False,
            ),
            "call_id": call_id,
        }
    if tool in {"edit_file", "write_file", "multi_edit_file", "delete_file", "move_file"}:
        # MVP: not implemented — we'd need apply_patch grammar. Defer to spec 002.
        # Render a placeholder shell command so the trace is at least readable.
        path = args.get("path", "?")
        return {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps(
                {
                    "cmd": f"echo '(translated {tool} on {path} not yet implemented in MVP)'",
                    "workdir": cwd,
                    "yield_time_ms": 1000,
                },
                ensure_ascii=False,
            ),
            "call_id": call_id,
        }
    # Fallback: passthrough as exec_command echo so call_id pairing remains intact
    return {
        "type": "function_call",
        "name": "exec_command",
        "arguments": json.dumps(
            {
                "cmd": f"echo '(translated {tool} call; canonical args: {json.dumps(args, ensure_ascii=False)})'",
                "workdir": cwd,
                "yield_time_ms": 1000,
            },
            ensure_ascii=False,
        ),
        "call_id": call_id,
    }


def render_tool_result(call_id: str, output_text: str, is_error: bool) -> dict[str, Any]:
    """Return the Codex response_item.payload for a tool_result."""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output_text,
    }


def _build_rg_command(args: dict[str, Any]) -> str:
    parts = ["rg"]
    if args.get("output_mode") == "files_with_matches":
        parts.append("-l")
    elif args.get("output_mode") == "count":
        parts.append("-c")
    if args.get("-i"):
        parts.append("-i")
    if args.get("multiline"):
        parts.extend(["-U", "--multiline-dotall"])
    for f in ("-A", "-B", "-C"):
        if f in args:
            parts.extend([f, str(args[f])])
    if "context" in args:
        parts.extend(["-C", str(args["context"])])
    if "type" in args:
        parts.extend(["--type", str(args["type"])])
    if "glob" in args:
        parts.extend(["-g", shlex.quote(args["glob"])])
    parts.append(shlex.quote(args.get("pattern", "")))
    parts.append(shlex.quote(args.get("path", ".")))
    cmd = " ".join(parts)
    if "head_limit" in args:
        cmd += f" | head -n {int(args['head_limit'])}"
    if "offset" in args:
        cmd += f" | tail -n +{int(args['offset'])}"
    return cmd
