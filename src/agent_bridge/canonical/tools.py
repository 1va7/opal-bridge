"""Canonical tool name registry.

See docs/ARCHITECTURE.md §3.4. Each canonical tool has a snake_case name
and a JSON-Schema-like input shape. Adapters translate native tool names
to and from these.
"""

from __future__ import annotations

CANONICAL_TOOLS: dict[str, dict[str, str]] = {
    "shell": {"description": "Run a shell command"},
    "read_file": {"description": "Read a file (text/image/notebook/pdf)"},
    "write_file": {"description": "Write or overwrite a file"},
    "edit_file": {"description": "Replace one occurrence in a file"},
    "multi_edit_file": {"description": "Multiple edits in one file"},
    "delete_file": {"description": "Delete a file"},
    "move_file": {"description": "Rename or move a file"},
    "find_files": {"description": "Glob filenames"},
    "search_text": {"description": "Search regex across files"},
    "web_fetch": {"description": "Fetch a URL"},
    "web_search": {"description": "Web search"},
    "notebook_edit": {"description": "Edit a Jupyter notebook"},
    "update_plan": {"description": "Update todo / plan list"},
    "worktree_enter": {"description": "Enter a git worktree"},
    "worktree_exit": {"description": "Exit a git worktree"},
    "ask_user": {"description": "Ask the user a question"},
    "view_image": {"description": "Load an image file for the model"},
    "subagent_dispatch": {"description": "Spawn a subagent"},
    "skill_invoke": {"description": "Invoke a skill"},
    "mcp_call": {"description": "Call an MCP-provided tool"},
    "schedule_task": {"description": "Schedule a recurring task (lossy)"},
}


def is_canonical(name: str) -> bool:
    return name in CANONICAL_TOOLS
