"""Helpers to list Claude Code sessions on disk.

Avoids reading whole files; only inspects head ~64KB to extract metadata.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


CC_PROJECTS = Path.home() / ".claude" / "projects"


@dataclass
class SessionSummary:
    session_id: str
    path: Path
    cwd: str | None
    git_branch: str | None
    first_prompt: str | None
    line_count: int
    tools: list[str]  # unique tool names seen
    mtime: float
    bytes_size: int


def list_sessions(
    *,
    project_filter: str | None = None,
    limit: int = 20,
) -> list[SessionSummary]:
    """Return up-to-N most recent CC sessions, optionally filtered by cwd substring.

    Reads only enough of each jsonl to extract metadata + tool names.
    """
    if not CC_PROJECTS.exists():
        return []
    candidates: list[Path] = []
    for proj_dir in CC_PROJECTS.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            candidates.append(f)

    # Sort by mtime descending
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    out: list[SessionSummary] = []
    for path in candidates:
        if len(out) >= limit:
            break
        s = _summarize(path)
        if s is None:
            continue
        if project_filter and project_filter not in (s.cwd or ""):
            continue
        out.append(s)
    return out


def _summarize(path: Path, *, head_bytes: int = 65536) -> SessionSummary | None:
    """Build a SessionSummary by reading the head of the jsonl."""
    try:
        st = path.stat()
        with path.open(encoding="utf-8", errors="replace") as f:
            head = f.read(head_bytes)
    except OSError:
        return None

    cwd: str | None = None
    git_branch: str | None = None
    first_prompt: str | None = None
    tools: set[str] = set()
    seen_user = False

    for line in head.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cwd is None and "cwd" in ev:
            cwd = ev["cwd"]
        if git_branch is None and "gitBranch" in ev:
            git_branch = ev["gitBranch"]
        if (
            first_prompt is None
            and ev.get("type") == "user"
            and not ev.get("isCompactSummary")
            and not seen_user
        ):
            content = ev.get("message", {}).get("content")
            if isinstance(content, str):
                first_prompt = content[:120]
                seen_user = True
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "text":
                        first_prompt = b.get("text", "")[:120]
                        seen_user = True
                        break
        if ev.get("type") == "assistant":
            blocks = ev.get("message", {}).get("content", [])
            if isinstance(blocks, list):
                for b in blocks:
                    if b.get("type") == "tool_use":
                        tools.add(b.get("name", "?"))

    # Real line count: small enough that scanning bytes for newlines is fast even on 10MB
    line_count = _count_lines(path)

    if not cwd:
        try:
            tail = _read_tail_lines(path, 4)
            for line in tail:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and "cwd" in ev:
                    cwd = ev["cwd"]
        except OSError:
            pass

    return SessionSummary(
        session_id=path.stem,
        path=path,
        cwd=cwd,
        git_branch=git_branch,
        first_prompt=first_prompt,
        line_count=line_count,
        tools=sorted(tools),
        mtime=st.st_mtime,
        bytes_size=st.st_size,
    )


def _count_lines(path: Path) -> int:
    """Count newline-terminated lines without loading whole file as text."""
    n = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 20)  # 1MB
            if not chunk:
                break
            n += chunk.count(b"\n")
    return n


def _read_tail_lines(path: Path, n: int, max_bytes: int = 16384) -> list[str]:
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as f:
        f.seek(start)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-n:]


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def fmt_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
