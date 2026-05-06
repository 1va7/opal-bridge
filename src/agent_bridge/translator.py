"""Top-level translator pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import claude_code, codex


_INGEST_OPTS = {"follow_subagents", "fidelity"}
_RENDER_OPTS = {
    "fidelity",
    "subagent_strategy",
    "model_name",
    "model_provider",
    "timezone_name",
    "cc_version",
}


def translate(
    *,
    source_path: Path | str,
    source_harness: str,
    target_harness: str,
    target_dir: Path | str | None = None,
    **opts: Any,
):
    if source_harness == "claude-code" and target_harness == "codex":
        canonical = claude_code.ingest(source_path, **{k: v for k, v in opts.items() if k in _INGEST_OPTS})
        return codex.render(canonical, target_dir=target_dir, **{k: v for k, v in opts.items() if k in _RENDER_OPTS})
    if source_harness == "codex" and target_harness == "claude-code":
        canonical = codex.ingest(source_path, **{k: v for k, v in opts.items() if k in _INGEST_OPTS})
        return claude_code.render(canonical, target_dir=target_dir, **{k: v for k, v in opts.items() if k in _RENDER_OPTS})
    raise NotImplementedError(
        f"Direction not implemented: {source_harness} → {target_harness}. "
        f"See specs/003-bidirectional.md for status."
    )
