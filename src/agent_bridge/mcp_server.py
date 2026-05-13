"""MCP server exposing agent-bridge tools to any MCP-aware client.

Run via:
    agent-resume mcp serve

Configuration in any MCP host (Claude Desktop, Cursor, Cline, Codex, ...):
    {
      "mcpServers": {
        "agent-bridge": {
          "command": "/abs/path/to/.venv/bin/python",
          "args": ["-m", "agent_bridge.cli", "mcp", "serve"]
        }
      }
    }

Exposed tools:
  - list_sessions          List recent CC or Codex sessions on disk.
  - translate_session      Translate one session jsonl across harnesses.
  - sync_now               Mirror recent sessions both ways (or one).
  - find_session           Resolve a session id to its on-disk file + harness.
  - prepare_resume         Translate (if needed) and return the native resume cmd.
  - resume_with_prompt     Translate + actually run resume non-interactively;
                           return model output.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .adapters.claude_code.listing import list_sessions as _cc_list
from .adapters.claude_code.render import CC_PROJECTS, encode_cwd
from .adapters.codex.paths import CODEX_HOME
from .sync import (
    _is_agent_bridge_cc,
    _is_agent_bridge_codex,
    sync_once,
)
from .translator import translate as _translate


logger = logging.getLogger(__name__)


def build_app() -> FastMCP:
    app = FastMCP("agent-bridge")

    @app.tool()
    def list_sessions(
        harness: str = "claude-code",
        days: int = 7,
        limit: int = 20,
        include_translated: bool = False,
    ) -> dict:
        """List recent CC or Codex sessions on disk.

        harness: 'claude-code' or 'codex'
        days: only sessions modified within N days
        limit: max rows
        include_translated: include sessions agent-bridge generated (default: hide them)

        Returns {harness, count, sessions: [...]} so clients always get a single
        content block.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        if harness == "claude-code":
            sessions = _list_cc(cutoff, limit, include_translated)
        elif harness == "codex":
            sessions = _list_codex(cutoff, limit, include_translated)
        else:
            raise ValueError(f"unknown harness: {harness}")
        return {"harness": harness, "count": len(sessions), "sessions": sessions}

    @app.tool()
    def translate_session(
        source_path: str,
        from_harness: str,
        to_harness: str,
        subagent_strategy: str = "inline",
    ) -> dict:
        """Translate one session jsonl. Returns the target session id, path,
        and the native resume command.
        """
        src = Path(source_path).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"source not found: {src}")
        res = _translate(
            source_path=src,
            source_harness=from_harness,
            target_harness=to_harness,
            subagent_strategy=subagent_strategy,
        )
        return {
            "target_session_id": res.session_id,
            "target_path": str(res.primary_path),
            "resume_command": res.resume_command,
            "warnings": list(res.warnings),
        }

    @app.tool()
    def sync_now(
        direction: str = "both",
        days: int = 7,
        max_bytes: int = 25 * 1024 * 1024,
        include_active: bool = False,
        force: bool = False,
    ) -> dict:
        """Mirror recent sessions in both directions (or one).

        direction: 'cc-to-codex' | 'codex-to-cc' | 'both'
        """
        stats = sync_once(
            direction=direction,
            days=days,
            max_bytes=max_bytes,
            include_active=include_active,
            force=force,
            log=lambda *_: None,
        )
        return {
            "translated": stats.translated,
            "skipped_existing": stats.skipped_existing,
            "skipped_active": stats.skipped_active,
            "skipped_too_big": stats.skipped_too_big,
            "skipped_empty": stats.skipped_empty,
            "failed": stats.failed,
            "failures": [{"source": s, "error": e} for s, e in stats.failures[:10]],
        }

    @app.tool()
    def find_session(session_id: str) -> dict:
        """Locate a session id on disk. Returns {path, harness, cwd, mtime}.

        Searches both ~/.claude/projects/ and ~/.codex/sessions/.
        """
        loc = _find_on_disk(session_id)
        if loc is None:
            raise ValueError(f"session not found: {session_id}")
        path, harness, cwd, mtime = loc
        return {
            "path": str(path),
            "harness": harness,
            "cwd": cwd,
            "mtime": mtime,
        }

    @app.tool()
    def prepare_resume(
        target_harness: str,
        session_id: str,
    ) -> dict:
        """Resolve a session id (from any harness), translate it to the target
        harness if needed, and return the native resume command + cwd.
        """
        loc = _find_on_disk(session_id)
        if loc is None:
            raise ValueError(f"session not found: {session_id}")
        src_path, src_harness, cwd, _ = loc

        if src_harness == target_harness:
            cmd = _native_resume_cmd(target_harness, session_id, cwd)
            return {
                "target_session_id": session_id,
                "target_path": str(src_path),
                "harness": target_harness,
                "resume_command": cmd,
                "cwd": cwd,
                "translated": False,
            }

        res = _translate(
            source_path=src_path,
            source_harness=src_harness,
            target_harness=target_harness,
        )
        return {
            "target_session_id": res.session_id,
            "target_path": str(res.primary_path),
            "harness": target_harness,
            "resume_command": _native_resume_cmd(target_harness, res.session_id, cwd),
            "cwd": cwd,
            "translated": True,
            "warnings": list(res.warnings),
        }

    @app.tool()
    def resume_with_prompt(
        target_harness: str,
        session_id: str,
        prompt: str,
        timeout_sec: int = 180,
    ) -> dict:
        """Translate (if needed) and actually run the resume command non-interactively.

        Returns {output, exit_code, target_session_id}.
        """
        meta = prepare_resume(target_harness=target_harness, session_id=session_id)
        cwd = meta["cwd"]
        target_id = meta["target_session_id"]

        if target_harness == "claude-code":
            cmd = ["claude", "--resume", target_id, "-p", prompt]
        elif target_harness == "codex":
            cmd = [
                "codex", "exec", "resume", target_id, prompt,
                "--skip-git-repo-check",
            ]
        else:
            raise ValueError(f"unknown target_harness: {target_harness}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=cwd if target_harness == "claude-code" else None,
            )
        except subprocess.TimeoutExpired:
            return {
                "output": "",
                "stderr": f"timed out after {timeout_sec}s",
                "exit_code": -1,
                "target_session_id": target_id,
                "command": " ".join(shlex.quote(c) for c in cmd),
            }
        return {
            "output": proc.stdout[-8000:],
            "stderr": proc.stderr[-2000:] if proc.returncode != 0 else "",
            "exit_code": proc.returncode,
            "target_session_id": target_id,
            "command": " ".join(shlex.quote(c) for c in cmd),
            "translated": meta.get("translated", False),
        }

    return app


# ---------- helpers ---------- #


def _list_cc(cutoff: float, limit: int, include_translated: bool) -> list[dict]:
    out: list[dict] = []
    if not CC_PROJECTS.exists():
        return out
    candidates: list[Path] = []
    for proj in CC_PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            candidates.append(f)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        if not include_translated and _is_agent_bridge_cc(p):
            continue
        cwd, first_prompt = _peek_cc(p)
        out.append({
            "session_id": p.stem,
            "harness": "claude-code",
            "path": str(p),
            "cwd": cwd,
            "first_prompt": first_prompt,
            "size_bytes": st.st_size,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
        if len(out) >= limit:
            break
    return out


def _list_codex(cutoff: float, limit: int, include_translated: bool) -> list[dict]:
    out: list[dict] = []
    sess_root = CODEX_HOME / "sessions"
    if not sess_root.exists():
        return out
    candidates = sorted(
        sess_root.glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in candidates:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        if not include_translated and _is_agent_bridge_codex(p):
            continue
        cwd, first_prompt, sid = _peek_codex(p)
        out.append({
            "session_id": sid or p.stem,
            "harness": "codex",
            "path": str(p),
            "cwd": cwd,
            "first_prompt": first_prompt,
            "size_bytes": st.st_size,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
        if len(out) >= limit:
            break
    return out


def _peek_cc(path: Path, max_bytes: int = 65536) -> tuple[str | None, str | None]:
    cwd = first_prompt = None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            head = f.read(max_bytes)
    except OSError:
        return cwd, first_prompt
    for line in head.splitlines()[:30]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cwd is None and "cwd" in ev:
            cwd = ev["cwd"]
        if (
            first_prompt is None
            and ev.get("type") == "user"
            and not ev.get("isCompactSummary")
        ):
            content = ev.get("message", {}).get("content")
            if isinstance(content, str):
                first_prompt = content[:200]
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "text":
                        first_prompt = b.get("text", "")[:200]
                        break
        if cwd and first_prompt:
            break
    return cwd, first_prompt


def _peek_codex(path: Path, max_bytes: int = 16384) -> tuple[str | None, str | None, str | None]:
    cwd = first_prompt = sid = None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            head = f.read(max_bytes)
    except OSError:
        return cwd, first_prompt, sid
    for line in head.splitlines()[:20]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "session_meta":
            p = ev.get("payload", {}) or {}
            if cwd is None:
                cwd = p.get("cwd")
            if sid is None:
                sid = p.get("id")
        elif t == "response_item":
            payload = ev.get("payload", {}) or {}
            if (
                first_prompt is None
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                content = payload.get("content", [])
                for b in content:
                    if b.get("type") == "input_text":
                        first_prompt = b.get("text", "")[:200]
                        break
        if cwd and first_prompt and sid:
            break
    return cwd, first_prompt, sid


def _find_on_disk(session_id: str) -> tuple[Path, str, str | None, float] | None:
    """Resolve a session id to (path, harness, cwd, mtime). None if not found."""
    # CC side
    if CC_PROJECTS.exists():
        for proj in CC_PROJECTS.iterdir():
            if not proj.is_dir():
                continue
            f = proj / f"{session_id}.jsonl"
            if f.exists():
                cwd, _ = _peek_cc(f)
                return f, "claude-code", cwd, f.stat().st_mtime
    # Codex side: search by filename suffix containing the id
    sess_root = CODEX_HOME / "sessions"
    if sess_root.exists():
        for f in sess_root.glob(f"**/rollout-*-{session_id}.jsonl"):
            cwd, _, _ = _peek_codex(f)
            return f, "codex", cwd, f.stat().st_mtime
        # Fallback: scan all and check session_meta.payload.id (slow)
        for f in sess_root.glob("**/rollout-*.jsonl"):
            _, _, sid = _peek_codex(f)
            if sid == session_id:
                cwd, _, _ = _peek_codex(f)
                return f, "codex", cwd, f.stat().st_mtime
    return None


def _native_resume_cmd(harness: str, session_id: str, cwd: str | None) -> str:
    if harness == "claude-code":
        wd_hint = f"cd {shlex.quote(cwd)} && " if cwd else ""
        return f"{wd_hint}claude --resume {session_id}"
    if harness == "codex":
        return f"codex resume {session_id}"
    raise ValueError(f"unknown harness: {harness}")


def serve() -> None:
    """Run stdio MCP server."""
    app = build_app()
    app.run()


if __name__ == "__main__":
    serve()
