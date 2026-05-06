"""Unit tests for install-hook (CC and Codex)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomllib
from typer.testing import CliRunner

from agent_bridge.cli import app


runner = CliRunner()


def test_install_hook_codex_preserves_existing_notify(tmp_path, monkeypatch) -> None:
    """When ~/.codex/config.toml already has a notify chain, our cmd should
    be appended to it inside the same `sh -c` script, not overwrite."""
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    cfg = fake_home / ".codex" / "config.toml"
    cfg.write_text(
        'notify = ["sh", "-c", "afplay /tmp/x.aiff >/dev/null 2>&1"]\n'
        'model = "gpt-5.5"\n'
    )
    monkeypatch.setenv("HOME", str(fake_home))
    # Force Path.home() to honor it on platforms where HOME isn't authoritative
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = runner.invoke(app, ["install-hook", "--target", "codex"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(cfg.read_text())
    notify = parsed["notify"]
    assert isinstance(notify, list)
    assert notify[0] == "sh" and notify[1] == "-c"
    inner = notify[2]
    assert "afplay /tmp/x.aiff" in inner
    assert "agent_bridge.cli sync" in inner
    assert "; " in inner  # chained
    # Other config preserved
    assert parsed["model"] == "gpt-5.5"
    # Backup created
    assert (fake_home / ".codex" / "config.toml.bak").exists()


def test_install_hook_codex_idempotent(tmp_path, monkeypatch) -> None:
    """Re-running install-hook codex shouldn't keep duplicating the sync cmd."""
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    cfg = fake_home / ".codex" / "config.toml"
    cfg.write_text('notify = ["sh", "-c", "afplay /tmp/x.aiff"]\n')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    r1 = runner.invoke(app, ["install-hook", "--target", "codex"])
    r2 = runner.invoke(app, ["install-hook", "--target", "codex"])
    assert r1.exit_code == 0
    assert r2.exit_code == 0
    # Second run should be a no-op
    assert "already chains" in r2.output.lower() or "nothing to do" in r2.output.lower()
    parsed = tomllib.loads(cfg.read_text())
    inner = parsed["notify"][2]
    # exactly ONE occurrence of our cmd
    assert inner.count("agent_bridge.cli sync") == 1


def test_install_hook_cc_appends_stop_hook(tmp_path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "afplay /tmp/x.aiff"}
    ]}]}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = runner.invoke(app, ["install-hook", "--target", "claude-code"])
    assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    cmds = data["hooks"]["Stop"][0]["hooks"]
    # original + new = 2
    assert len(cmds) == 2
    assert any("agent_bridge.cli sync" in c["command"] for c in cmds)


def test_install_hook_cc_idempotent(tmp_path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    r1 = runner.invoke(app, ["install-hook", "--target", "claude-code"])
    r2 = runner.invoke(app, ["install-hook", "--target", "claude-code"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert "already installed" in r2.output.lower() or "nothing to do" in r2.output.lower()
    data = json.loads(settings.read_text())
    cmds = data["hooks"]["Stop"][0]["hooks"]
    assert sum("agent_bridge.cli sync" in c["command"] for c in cmds) == 1
