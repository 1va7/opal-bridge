"""apply_patch grammar generator and file-state tracker.

Codex's `apply_patch` is the only file-editing primitive on the Codex side.
We translate CC's Edit/Write/MultiEdit/Delete/Move into apply_patch input
strings following the Lark grammar at codex-rs/tools/src/tool_apply_patch.lark.

To generate Update hunks with ≤3 lines of context, we need to know the
file's content at the time of the edit. We build that knowledge by walking
the moment sequence in order:
  - read_file ToolResult → cache content (after stripping cat -n line numbers)
  - write_file ToolCall  → cache content from args.content
  - edit_file ToolCall   → simulate edit to keep cache fresh
  - delete_file ToolCall → drop from cache
  - move_file ToolCall   → rename in cache
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from agent_bridge.canonical.schema import Moment, ToolCall, ToolResult


@dataclass
class FileStateCache:
    """File path → known content (best-effort, MVP). Paths stored as-given."""

    contents: dict[str, str] = field(default_factory=dict)

    def get(self, path: str) -> str | None:
        return self.contents.get(path)

    def set(self, path: str, content: str) -> None:
        self.contents[path] = content

    def drop(self, path: str) -> None:
        self.contents.pop(path, None)

    def rename(self, old: str, new: str) -> None:
        if old in self.contents:
            self.contents[new] = self.contents.pop(old)


_LINENO_RE = re.compile(r"^\s*\d+\t")


def strip_cat_n(text: str) -> str:
    """Strip `cat -n`-style line number prefixes that CC's Read returns."""
    out_lines = []
    for ln in text.splitlines():
        m = _LINENO_RE.match(ln)
        if m:
            out_lines.append(ln[m.end():])
        else:
            out_lines.append(ln)
    # Preserve trailing newline if original had one
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(out_lines) + suffix


def build_file_state(moments: Iterable[Moment]) -> dict[str, FileStateCache]:
    """Walk moments and produce a per-call_id snapshot map.

    Returns a dict keyed by ToolCall.call_id of edit_file/write_file/...
    each value is a FileStateCache reflecting state JUST BEFORE that call.
    Used so the renderer can grab context lines without re-walking.
    """
    snapshots: dict[str, FileStateCache] = {}
    cache = FileStateCache()
    pending_reads: dict[str, str] = {}  # call_id → path

    for m in moments:
        if isinstance(m, ToolCall):
            if m.tool == "read_file":
                pending_reads[m.call_id] = m.args.get("path", "") or ""
            elif m.tool in {"write_file", "edit_file", "multi_edit_file", "delete_file", "move_file"}:
                # snapshot current state for this call
                snap = FileStateCache(contents=dict(cache.contents))
                snapshots[m.call_id] = snap
                _apply_edit_to_cache(cache, m)
        elif isinstance(m, ToolResult) and m.call_id in pending_reads:
            path = pending_reads.pop(m.call_id)
            cache.set(path, strip_cat_n(m.output_text))

    return snapshots


def _apply_edit_to_cache(cache: FileStateCache, call: ToolCall) -> None:
    """Mutate cache to reflect an edit/write/etc. (best-effort)."""
    args = call.args
    path = args.get("path", "")
    if not path:
        return
    if call.tool == "write_file":
        cache.set(path, args.get("content", ""))
    elif call.tool == "edit_file":
        old = args.get("old", "")
        new = args.get("new", "")
        cur = cache.get(path)
        if cur is not None and old in cur:
            cache.set(path, cur.replace(old, new, 1 if not args.get("replace_all") else -1))
    elif call.tool == "multi_edit_file":
        cur = cache.get(path)
        if cur is not None:
            for ed in args.get("edits", []):
                old = ed.get("old", "")
                new = ed.get("new", "")
                if old in cur:
                    cur = cur.replace(old, new, 1)
            cache.set(path, cur)
    elif call.tool == "delete_file":
        cache.drop(path)
    elif call.tool == "move_file":
        cache.rename(args.get("from", path), args.get("to", path))


def to_relative(abs_path: str, cwd: str) -> str:
    """Compute path relative to cwd. apply_patch grammar requires relative."""
    try:
        return str(Path(abs_path).resolve().relative_to(Path(cwd).resolve()))
    except (ValueError, OSError):
        # Outside cwd — keep absolute and let the model figure it out.
        # apply_patch parser is lenient about this.
        return abs_path


# ---------- patch composition ---------- #


def compose_add_file(rel_path: str, content: str) -> str:
    body = []
    for ln in content.splitlines():
        body.append(f"+{ln}")
    if content == "" or content.endswith("\n"):
        # apply_patch expects no trailing empty +line for files ending in newline
        pass
    return f"*** Add File: {rel_path}\n" + "\n".join(body) + ("\n" if body else "")


def compose_delete_file(rel_path: str) -> str:
    return f"*** Delete File: {rel_path}\n"


def compose_update_file(
    rel_path: str,
    hunks: list[tuple[list[str], list[str], list[str]]],
    *,
    move_to: str | None = None,
) -> str:
    """hunks: list of (context_before, removed_lines, added_lines).
    Trailing context is omitted in MVP (apply_patch lenient mode tolerates it).
    """
    lines = [f"*** Update File: {rel_path}"]
    if move_to:
        lines.append(f"*** Move to: {move_to}")
    for ctx_before, removed, added in hunks:
        lines.append("@@")
        for c in ctx_before[-3:]:
            lines.append(f" {c}")
        for r in removed:
            lines.append(f"-{r}")
        for a in added:
            lines.append(f"+{a}")
    return "\n".join(lines) + "\n"


def compose_patch_envelope(body: str) -> str:
    if not body.endswith("\n"):
        body += "\n"
    return "*** Begin Patch\n" + body + "*** End Patch\n"


# ---------- per-tool patch building ---------- #


def patch_for_write(rel_path: str, content: str, *, file_existed: bool) -> str:
    if not file_existed:
        body = compose_add_file(rel_path, content)
    else:
        # Overwrite-existing: Delete + Add as a single envelope (atomic)
        body = compose_delete_file(rel_path) + compose_add_file(rel_path, content)
    return compose_patch_envelope(body)


def patch_for_edit(
    rel_path: str,
    old: str,
    new: str,
    *,
    file_content: str | None,
    replace_all: bool = False,
) -> str:
    """Generate an Update File patch for one Edit.

    If file_content is None we fall back to a contextless hunk (lenient
    parser will accept it as long as old is unique in the actual file).
    replace_all=True produces multiple hunks (one per occurrence in the
    cached content).
    """
    occurrences = [(0, 0)]
    if file_content is not None:
        occurrences = _find_all(file_content, old) if replace_all else _find_first(file_content, old)
        if not occurrences and not replace_all:
            # Fallback: contextless (model may still succeed if old is unique)
            occurrences = [(-1, -1)]

    hunks = []
    if file_content is None:
        hunks.append(([], old.splitlines(), new.splitlines()))
    else:
        for start, end in occurrences:
            if start < 0:
                hunks.append(([], old.splitlines(), new.splitlines()))
            else:
                pre = file_content[:start].splitlines()
                hunks.append((pre, old.splitlines(), new.splitlines()))

    body = compose_update_file(rel_path, hunks)
    return compose_patch_envelope(body)


def patch_for_multi_edit(
    rel_path: str,
    edits: list[dict],
    *,
    file_content: str | None,
) -> str:
    """Multiple hunks under one Update File header."""
    hunks: list[tuple[list[str], list[str], list[str]]] = []
    cur = file_content
    for ed in edits:
        old = ed.get("old_string", ed.get("old", ""))
        new = ed.get("new_string", ed.get("new", ""))
        if cur is None:
            hunks.append(([], old.splitlines(), new.splitlines()))
            continue
        idx = cur.find(old)
        if idx < 0:
            hunks.append(([], old.splitlines(), new.splitlines()))
            continue
        pre = cur[:idx].splitlines()
        hunks.append((pre, old.splitlines(), new.splitlines()))
        cur = cur[:idx] + new + cur[idx + len(old):]
    body = compose_update_file(rel_path, hunks)
    return compose_patch_envelope(body)


def patch_for_delete(rel_path: str) -> str:
    return compose_patch_envelope(compose_delete_file(rel_path))


def patch_for_move(old_rel: str, new_rel: str) -> str:
    return compose_patch_envelope(compose_update_file(old_rel, [], move_to=new_rel))


def _find_first(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    i = haystack.find(needle)
    return [(i, i + len(needle))] if i >= 0 else []


def _find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    out = []
    if not needle:
        return out
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            break
        out.append((i, i + len(needle)))
        start = i + len(needle)
    return out
