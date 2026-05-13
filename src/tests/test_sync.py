from __future__ import annotations

import json
import importlib
from pathlib import Path

from agent_bridge import sync_state
from agent_bridge.adapters.claude_code.render import encode_cwd
from agent_bridge.sync import _expected_target, sync_once


def _write_cc_session(path: Path, *, session_id: str = "cc-sync-1") -> None:
    rows = [
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": "u1",
            "parentUuid": None,
            "cwd": "/tmp/project",
            "timestamp": "2026-05-13T01:00:00.000Z",
            "message": {"role": "user", "content": "first prompt"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": "a1",
            "parentUuid": "u1",
            "cwd": "/tmp/project",
            "timestamp": "2026-05-13T01:00:01.000Z",
            "message": {"role": "assistant", "content": "first answer"},
        },
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": "u2",
            "parentUuid": "a1",
            "cwd": "/tmp/project",
            "timestamp": "2026-05-13T01:00:02.000Z",
            "message": {"role": "user", "content": "second prompt"},
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _isolate_sync_paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    import agent_bridge.pair_map as pair_map
    import agent_bridge.sync as sync_mod
    from agent_bridge.adapters.codex import manifest
    from agent_bridge.adapters.codex import paths as codex_paths
    cc_render = importlib.import_module("agent_bridge.adapters.claude_code.render")
    codex_render = importlib.import_module("agent_bridge.adapters.codex.render")

    cc_root = tmp_path / "home" / ".claude" / "projects"
    codex_home = tmp_path / "home" / ".codex"
    cache_root = tmp_path / "cache"

    monkeypatch.setattr(sync_mod, "CC_PROJECTS", cc_root)
    monkeypatch.setattr(cc_render, "CC_PROJECTS", cc_root)
    monkeypatch.setattr(sync_mod, "CODEX_HOME", codex_home)
    monkeypatch.setattr(codex_paths, "CODEX_HOME", codex_home)
    monkeypatch.setattr(codex_render, "CODEX_HOME", codex_home)
    monkeypatch.setattr(sync_state, "STATE_PATH", cache_root / "sync-state.json")
    monkeypatch.setattr(manifest, "MANIFEST_PATH", cache_root / "codex-write-manifest.json")
    monkeypatch.setattr(pair_map, "PAIR_MAP_PATH", cache_root / "pair-map.json")

    return cc_root, codex_home


def test_sync_rewrites_missing_target_even_when_source_marked_unchanged(tmp_path: Path, monkeypatch) -> None:
    cc_root, _ = _isolate_sync_paths(tmp_path, monkeypatch)
    source = cc_root / encode_cwd("/tmp/project") / "cc-sync-1.jsonl"
    _write_cc_session(source)

    # Simulate stale state from a previous successful run whose target file
    # was later deleted or generated under an older mapping.
    sync_state.mark_translated(source, "claude-code-to-codex", "")
    target_path, _ = _expected_target(source, "claude-code", "codex")
    assert not target_path.exists()

    stats = sync_once(direction="cc-to-codex", days=365, log=lambda *_: None)

    assert stats.translated == 1
    assert stats.skipped_existing == 0
    assert target_path.exists()


def test_sync_force_rewrites_existing_short_target(tmp_path: Path, monkeypatch) -> None:
    cc_root, _ = _isolate_sync_paths(tmp_path, monkeypatch)
    source = cc_root / encode_cwd("/tmp/project") / "cc-sync-1.jsonl"
    _write_cc_session(source)

    first = sync_once(direction="cc-to-codex", days=365, log=lambda *_: None)
    assert first.translated == 1
    target_path, _ = _expected_target(source, "claude-code", "codex")
    full_size = target_path.stat().st_size
    target_path.write_text(target_path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    assert target_path.stat().st_size < full_size

    skipped = sync_once(direction="cc-to-codex", days=365, log=lambda *_: None)
    assert skipped.translated == 0
    assert skipped.skipped_existing == 1
    assert target_path.stat().st_size < full_size

    repaired = sync_once(direction="cc-to-codex", days=365, force=True, log=lambda *_: None)
    assert repaired.translated == 1
    assert target_path.stat().st_size == full_size


def test_sync_skips_empty_source_and_removes_generated_target(tmp_path: Path, monkeypatch) -> None:
    _, codex_home = _isolate_sync_paths(tmp_path, monkeypatch)
    source = (
        codex_home
        / "sessions"
        / "2026"
        / "05"
        / "13"
        / "rollout-2026-05-13T01-00-00-019e1f00-0000-7000-8000-000000000001.jsonl"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-05-13T01:00:00.000Z",
                "payload": {
                    "id": "019e1f00-0000-7000-8000-000000000001",
                    "cwd": "/tmp/project",
                    "source": "cli",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    target_path, target_id = _expected_target(source, "codex", "claude-code")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(
            {
                "type": "custom-title",
                "customTitle": "[from codex] empty",
                "sessionId": target_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    stats = sync_once(direction="codex-to-cc", days=365, force=True, log=lambda *_: None)

    assert stats.translated == 0
    assert stats.skipped_empty == 1
    assert not target_path.exists()
