"""CC tool name → canonical name + arg translation.

See docs/tool-mapping.md §3.
"""

from __future__ import annotations

from typing import Any


CC_TO_CANONICAL: dict[str, str] = {
    "Bash": "shell",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "multi_edit_file",
    "Glob": "find_files",
    "Grep": "search_text",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "NotebookEdit": "notebook_edit",
    "TaskCreate": "update_plan",
    "TaskUpdate": "update_plan",
    "TaskList": "update_plan",
    "TaskGet": "update_plan",
    "TaskOutput": "update_plan",
    "TaskStop": "update_plan",
    "Agent": "subagent_dispatch",
    "Skill": "skill_invoke",
    "AskUserQuestion": "ask_user",
    "EnterWorktree": "worktree_enter",
    "ExitWorktree": "worktree_exit",
    "ScheduleWakeup": "schedule_task",
    "CronCreate": "schedule_task",
    "CronList": "schedule_task",
    "CronDelete": "schedule_task",
}


def cc_tool_to_canonical(cc_name: str) -> str:
    """Map CC's wire tool name to canonical. Unknown names → wire-name itself (passthrough)."""
    if cc_name.startswith("mcp__"):
        return "mcp_call"
    return CC_TO_CANONICAL.get(cc_name, cc_name)


def translate_args(canonical_name: str, cc_input: dict[str, Any]) -> dict[str, Any]:
    """Translate CC input dict to canonical args dict.

    For MVP we keep most fields unchanged — adapters use `wire_native` to
    preserve the original for round-trip. Only normalize obviously wrong
    fields.
    """
    if canonical_name == "shell":
        return {
            "command": cc_input.get("command", ""),
            "description": cc_input.get("description"),
            "timeout_ms": cc_input.get("timeout"),
            "run_in_background": cc_input.get("run_in_background", False),
        }
    if canonical_name == "read_file":
        return {
            "path": cc_input.get("file_path", ""),
            "offset": cc_input.get("offset"),
            "limit": cc_input.get("limit"),
        }
    if canonical_name == "edit_file":
        return {
            "path": cc_input.get("file_path", ""),
            "old": cc_input.get("old_string", ""),
            "new": cc_input.get("new_string", ""),
            "replace_all": cc_input.get("replace_all", False),
        }
    if canonical_name == "write_file":
        return {
            "path": cc_input.get("file_path", ""),
            "content": cc_input.get("content", ""),
            "overwrite": True,
        }
    if canonical_name == "find_files":
        return {
            "pattern": cc_input.get("pattern", ""),
            "path": cc_input.get("path"),
        }
    if canonical_name == "search_text":
        # Keep all rg-ish args for the renderer to assemble
        return dict(cc_input)
    # default: passthrough
    return dict(cc_input)
