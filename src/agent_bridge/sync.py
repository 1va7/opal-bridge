"""Batch and watch sync — keep CC and Codex session jsonls mirrored.

Two operations:
  - `sync_once`: walk recent sessions on either side, translate any whose
    source mtime is newer than the existing target. Idempotent thanks to
    deterministic target UUIDs.
  - `watch_loop`: poll every N seconds, calling sync_once each time.

Skipped:
  - Currently-active CC sessions (`~/.claude/sessions/<pid>.json` registry)
  - Files larger than `--max-bytes` (configurable, default 25MB)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_bridge.adapters import claude_code, codex
from agent_bridge.adapters.codex.paths import CODEX_HOME
from agent_bridge.adapters.claude_code.render import CC_PROJECTS, encode_cwd
from agent_bridge.canonical.ids import deterministic_uuid4, deterministic_uuid7

logger = logging.getLogger(__name__)


@dataclass
class SyncStats:
    translated: int = 0
    skipped_existing: int = 0
    skipped_active: int = 0
    skipped_too_big: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = None  # list of (source_path, error)

    def __post_init__(self):
        if self.failures is None:
            self.failures = []


def sync_once(
    *,
    direction: str = "both",  # "cc-to-codex" | "codex-to-cc" | "both"
    days: int = 7,
    max_bytes: int = 25 * 1024 * 1024,
    log: callable = print,
) -> SyncStats:
    """Translate recent sessions on the requested direction(s)."""
    stats = SyncStats()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

    if direction in ("cc-to-codex", "both"):
        for path in _list_cc_sessions(cutoff):
            _try_translate_one(path, "claude-code", "codex", max_bytes, stats, log)

    if direction in ("codex-to-cc", "both"):
        for path in _list_codex_sessions(cutoff):
            _try_translate_one(path, "codex", "claude-code", max_bytes, stats, log)

    return stats


def watch_loop(
    *,
    interval: int = 30,
    direction: str = "both",
    days: int = 7,
    max_bytes: int = 25 * 1024 * 1024,
    log: callable = print,
) -> None:
    """Poll every `interval` seconds; never returns. Ctrl-C to stop."""
    log(f"agent-bridge watch: direction={direction}, interval={interval}s")
    while True:
        try:
            stats = sync_once(direction=direction, days=days, max_bytes=max_bytes, log=lambda *_: None)
            if stats.translated > 0 or stats.failed > 0:
                log(f"[{datetime.now().strftime('%H:%M:%S')}] +{stats.translated} translated, "
                    f"{stats.skipped_existing} unchanged, {stats.skipped_active} active, "
                    f"{stats.skipped_too_big} too big, {stats.failed} failed")
            time.sleep(interval)
        except KeyboardInterrupt:
            log("\nstopped.")
            return
        except Exception as e:
            log(f"watch error: {e}; sleeping {interval}s")
            time.sleep(interval)


def _try_translate_one(
    source_path: Path,
    src_harness: str,
    tgt_harness: str,
    max_bytes: int,
    stats: SyncStats,
    log: callable,
) -> None:
    try:
        size = source_path.stat().st_size
    except OSError:
        return
    if size > max_bytes:
        stats.skipped_too_big += 1
        return
    if src_harness == "claude-code" and _cc_session_is_active(source_path):
        stats.skipped_active += 1
        return

    target_path, target_id = _expected_target(source_path, src_harness, tgt_harness)
    if target_path.exists():
        try:
            target_mtime = target_path.stat().st_mtime
            source_mtime = source_path.stat().st_mtime
            if target_mtime >= source_mtime:
                stats.skipped_existing += 1
                return
        except OSError:
            pass

    try:
        ingest_fn = _ingest_for(src_harness)
        render_fn = _render_for(tgt_harness)
        canonical = ingest_fn(source_path)
        title_prefix = f"[from {src_harness}] " if tgt_harness == "claude-code" else None
        render_fn(
            canonical,
            session_id=target_id,
            title_prefix=title_prefix,
            subagent_strategy="inline",
        )
        stats.translated += 1
        log(f"  ✓ {src_harness}→{tgt_harness}: {source_path.name} → {target_id}")
    except Exception as e:
        stats.failed += 1
        stats.failures.append((str(source_path), str(e)))
        log(f"  ✗ {source_path.name}: {e}")


def _ingest_for(harness: str):
    if harness == "claude-code":
        return claude_code.ingest
    if harness == "codex":
        return codex.ingest
    raise ValueError(f"unknown source harness: {harness}")


def _render_for(harness: str):
    if harness == "claude-code":
        return claude_code.render
    if harness == "codex":
        return codex.render
    raise ValueError(f"unknown target harness: {harness}")


def _expected_target(
    source_path: Path,
    src_harness: str,
    tgt_harness: str,
) -> tuple[Path, str]:
    """Where would the rendered jsonl land if we translated it now?

    We need the cwd from the source jsonl head to compute encoded-cwd.
    """
    head = _read_head(source_path)
    if src_harness == "claude-code":
        # CC: any line has cwd; use the first one we find
        cwd = _first_cwd_cc(head)
        started_at = _first_timestamp(head)
        source_id = source_path.stem
        if tgt_harness == "codex":
            target_id = deterministic_uuid7(f"cc:{source_id}", started_at)
            from agent_bridge.adapters.codex.paths import codex_path
            return codex_path(target_id, started_at), target_id
        else:  # cc-to-cc unsupported
            raise ValueError("cc-to-cc not a real direction")
    else:  # codex source
        cwd = _first_cwd_codex(head)
        started_at = _first_timestamp(head)
        source_id = _codex_source_id(head, source_path)
        if tgt_harness == "claude-code":
            target_id = deterministic_uuid4(f"codex:{source_id}")
            encoded = encode_cwd(cwd or str(Path.home()))
            return CC_PROJECTS / encoded / f"{target_id}.jsonl", target_id
        else:
            raise ValueError("codex-to-codex not a real direction")


def _read_head(path: Path, max_lines: int = 10) -> list[dict]:
    out: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _first_cwd_cc(head: list[dict]) -> str | None:
    for ev in head:
        if "cwd" in ev:
            return ev["cwd"]
    return None


def _first_cwd_codex(head: list[dict]) -> str | None:
    for ev in head:
        if ev.get("type") == "session_meta":
            return ev.get("payload", {}).get("cwd")
    for ev in head:
        if ev.get("type") == "turn_context":
            return ev.get("payload", {}).get("cwd")
    return None


def _first_timestamp(head: list[dict]) -> str:
    for ev in head:
        ts = ev.get("timestamp")
        if ts:
            return ts
    return "2026-01-01T00:00:00.000Z"


def _codex_source_id(head: list[dict], path: Path) -> str:
    for ev in head:
        if ev.get("type") == "session_meta":
            sid = ev.get("payload", {}).get("id")
            if sid:
                return sid
    # Fallback: extract from filename
    name = path.stem
    if "-" in name:
        # rollout-2026-...-019df...; the last UUID-shaped segment is the id
        parts = name.split("-")
        # Find segments that look like a uuid (8-4-4-4-12)
        for start in range(len(parts) - 4):
            cand = "-".join(parts[start : start + 5])
            if len(cand) == 36 and cand.count("-") == 4:
                return cand
    return name


def _list_cc_sessions(cutoff_mtime: float) -> list[Path]:
    out: list[Path] = []
    if not CC_PROJECTS.exists():
        return out
    for proj in CC_PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff_mtime:
                    continue
            except OSError:
                continue
            if _is_agent_bridge_cc(f):
                continue
            out.append(f)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def _list_codex_sessions(cutoff_mtime: float) -> list[Path]:
    out: list[Path] = []
    sess_root = CODEX_HOME / "sessions"
    if not sess_root.exists():
        return out
    for f in sess_root.glob("**/rollout-*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff_mtime:
                continue
        except OSError:
            continue
        if _is_agent_bridge_codex(f):
            continue
        out.append(f)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


_AGENT_BRIDGE_MARKER = "agent-bridge"


def _is_agent_bridge_cc(path: Path) -> bool:
    """A CC jsonl produced by us has a `custom-title` line starting with `[from `,
    OR its filename matches deterministic_uuid4(seed='codex:<some real codex id>')
    for any codex session currently on disk.
    """
    if _is_agent_bridge_cc_by_marker(path):
        return True
    if path.stem in _agent_bridge_cc_uuids():
        return True
    return False


def _is_agent_bridge_cc_by_marker(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    for raw in head.splitlines()[:5]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "custom-title" and isinstance(ev.get("customTitle"), str):
            t = ev["customTitle"]
            if t.startswith("[from "):
                return True
    return False


_CC_UUID_CACHE: set[str] | None = None


def _agent_bridge_cc_uuids() -> set[str]:
    """Compute the set of CC UUIDs that would be produced by translating any
    on-disk Codex session via our deterministic mapping.

    Cached for the life of the process.
    """
    global _CC_UUID_CACHE
    if _CC_UUID_CACHE is not None:
        return _CC_UUID_CACHE
    out: set[str] = set()
    sess_root = CODEX_HOME / "sessions"
    if sess_root.exists():
        for cf in sess_root.glob("**/rollout-*.jsonl"):
            codex_id = _extract_codex_id_from_filename(cf.name)
            if codex_id:
                out.add(deterministic_uuid4(f"codex:{codex_id}"))
            # Also peek session_meta.payload.id (it's the authoritative source id)
            try:
                with cf.open(encoding="utf-8", errors="replace") as f:
                    first = f.readline().strip()
                if first:
                    ev = json.loads(first)
                    if ev.get("type") == "session_meta":
                        sid = ev.get("payload", {}).get("id")
                        if sid:
                            out.add(deterministic_uuid4(f"codex:{sid}"))
            except (OSError, json.JSONDecodeError):
                continue
    _CC_UUID_CACHE = out
    return out


def _extract_codex_id_from_filename(name: str) -> str | None:
    """rollout-2026-05-05T18-28-34-019df7ae-b036-76c2-a499-fe10385d955a.jsonl
       → '019df7ae-b036-76c2-a499-fe10385d955a'
    """
    base = name[: -len(".jsonl")] if name.endswith(".jsonl") else name
    parts = base.split("-")
    # The last 5 segments form the UUID (8-4-4-4-12 hex)
    if len(parts) < 5:
        return None
    cand = "-".join(parts[-5:])
    if len(cand) == 36 and cand.count("-") == 4:
        return cand
    return None


def _is_agent_bridge_codex(path: Path) -> bool:
    """A Codex rollout produced by us has session_meta.payload.originator == 'agent-bridge'.

    (The source field used to be Custom('agent-bridge'); v0.2 changed it to
    "cli" so codex's resume picker shows us. We now identify ourselves via
    `originator` instead — codex never uses that string itself.)
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
    except OSError:
        return False
    if not first:
        return False
    try:
        ev = json.loads(first)
    except json.JSONDecodeError:
        return False
    if ev.get("type") != "session_meta":
        return False
    payload = ev.get("payload") or {}
    if payload.get("originator") == _AGENT_BRIDGE_MARKER:
        return True
    # Backward-compat: also detect old source.custom == "agent-bridge"
    src = payload.get("source")
    if isinstance(src, dict) and src.get("custom") == _AGENT_BRIDGE_MARKER:
        return True
    return False


def _cc_session_is_active(path: Path) -> bool:
    """True if some CC process registry says this session is currently busy."""
    reg = Path.home() / ".claude" / "sessions"
    if not reg.exists():
        return False
    sid = path.stem
    for pid_file in reg.glob("*.json"):
        try:
            data = json.loads(pid_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("sessionId") == sid and data.get("status") in {"busy", "idle"}:
            return True
    return False
