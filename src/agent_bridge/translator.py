"""Top-level translator pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import claude_code, codex
from .adapters.codex.render import RenderResult


def translate(
    *,
    source_path: Path | str,
    source_harness: str,
    target_harness: str,
    target_dir: Path | str | None = None,
    **opts: Any,
) -> RenderResult:
    if source_harness == "claude-code" and target_harness == "codex":
        canonical = claude_code.ingest(source_path, **{k: v for k, v in opts.items() if k in {"follow_subagents", "fidelity"}})
        return codex.render(canonical, target_dir=target_dir, **{k: v for k, v in opts.items() if k in {"fidelity", "subagent_strategy", "model_name", "model_provider", "timezone_name"}})
    raise NotImplementedError(
        f"Direction not implemented in MVP: {source_harness} → {target_harness}. "
        f"See specs/001-translator-mvp.md §1.2."
    )
