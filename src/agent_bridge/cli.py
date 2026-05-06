"""Command-line interface."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

from .adapters.claude_code.listing import fmt_mtime, fmt_size, list_sessions
from .sync import sync_once, watch_loop
from .translator import translate


app = typer.Typer(no_args_is_help=True, add_completion=False, help="agent-bridge: cross-agent session translator")


@app.command("translate")
def translate_cmd(
    source_path: Path = typer.Argument(..., help="Source jsonl path (CC session file)"),
    from_: str = typer.Option("claude-code", "--from", help="Source harness"),
    to: str = typer.Option("codex", "--to", help="Target harness"),
    target_dir: Path = typer.Option(None, "--target-dir", help="Where to write the target session (defaults to ~/.codex/sessions for codex)"),
    model: str = typer.Option("gpt-5.5", "--model", help="Codex target model"),
    model_provider: str = typer.Option("openai", "--model-provider"),
    fidelity: str = typer.Option("A", "--fidelity", help="A=faithful (only one supported in MVP)"),
    subagent_strategy: str = typer.Option("drop", "--subagent-strategy", help="drop | inline (002c+)"),
) -> None:
    """Translate a session from one harness's format to another."""
    if fidelity != "A":
        typer.echo(f"warning: fidelity {fidelity} not implemented; using A", err=True)
    res = translate(
        source_path=source_path,
        source_harness=from_,
        target_harness=to,
        target_dir=target_dir,
        model_name=model,
        model_provider=model_provider,
        subagent_strategy=subagent_strategy,
    )
    typer.echo(f"session_id: {res.session_id}")
    typer.echo(f"output:     {res.primary_path}")
    if res.sidecar_paths:
        typer.echo("sidecars:")
        for p in res.sidecar_paths:
            typer.echo(f"  - {p}")
    if res.warnings:
        typer.echo("warnings:", err=True)
        for w in res.warnings:
            typer.echo(f"  ! {w}", err=True)
    typer.echo("")
    typer.echo("Resume command:")
    typer.echo(f"  {res.resume_command}")


@app.command("list")
def list_cmd(
    project: str = typer.Option(None, "--project", "-p", help="Filter by cwd substring"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows to show"),
    show_path: bool = typer.Option(False, "--paths", help="Show full jsonl paths"),
) -> None:
    """List recent Claude Code sessions on disk (most recent first)."""
    sessions = list_sessions(project_filter=project, limit=limit)
    if not sessions:
        typer.echo("(no Claude Code sessions found in ~/.claude/projects/)")
        raise typer.Exit(0)

    typer.echo(f"{'mtime':<12} {'size':<8} {'lines':<6} {'tools':<35} session_id")
    typer.echo("-" * 110)
    for s in sessions:
        tools = ", ".join(s.tools) if s.tools else "-"
        if len(tools) > 33:
            tools = tools[:30] + "..."
        first = (s.first_prompt or "").replace("\n", " ")[:60]
        typer.echo(
            f"{fmt_mtime(s.mtime):<12} {fmt_size(s.bytes_size):<8} {s.line_count:<6} {tools:<35} {s.session_id}"
        )
        if first:
            typer.echo(f"             ↳ cwd={s.cwd}  prompt='{first}'")
        if show_path:
            typer.echo(f"             ↳ path={s.path}")


@app.command("smoke")
def smoke_cmd(
    source_path: Path = typer.Argument(..., help="Source CC or Codex jsonl path"),
    from_: str = typer.Option("claude-code", "--from", help="Source harness: claude-code | codex"),
    to: str = typer.Option(None, "--to", help="Target harness (default: opposite of --from)"),
    prompt: str = typer.Option("Reply with: WORKS", "--prompt", help="Test prompt for resume"),
    model: str = typer.Option("gpt-5.5", "--model"),
    keep: bool = typer.Option(False, "--keep", help="Keep the translated jsonl after the test"),
) -> None:
    """Translate then immediately run resume to verify end-to-end."""
    if to is None:
        to = "codex" if from_ == "claude-code" else "claude-code"
    res = translate(
        source_path=source_path,
        source_harness=from_,
        target_harness=to,
        target_dir=None,
        model_name=model,
    )
    typer.echo(f"translated: {res.session_id}")
    typer.echo(f"file:       {res.primary_path}")

    # Build the resume command for the target harness
    if to == "codex":
        cmd = [
            "codex", "exec", "resume",
            res.session_id, prompt,
            "--skip-git-repo-check",
            "-o", "/tmp/agent-bridge-smoke.md",
        ]
    elif to == "claude-code":
        cmd = ["claude", "--resume", res.session_id, "-p", prompt]
    else:
        typer.echo(f"unknown target: {to}", err=True)
        raise typer.Exit(2)
    typer.echo(f"\nrunning: {' '.join(cmd)}\n")

    cwd_for_resume = None
    if to == "claude-code":
        # claude --resume scans the encoded-cwd of the process cwd; need to cd to session.cwd
        # We don't have the session here, but the rendered jsonl encodes it; pull from path:
        # path: ~/.claude/projects/<encoded>/<id>.jsonl — encoded reflects realpath(session.cwd)
        # Easier: read the first line of the rendered file
        try:
            first = next(open(res.primary_path), "{}")
            import json as _j
            cwd_for_resume = _j.loads(first).get("cwd")
        except Exception:
            pass

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=cwd_for_resume)
    except FileNotFoundError:
        typer.echo(f"ERROR: `{cmd[0]}` not on PATH.", err=True)
        raise typer.Exit(2)
    except subprocess.TimeoutExpired:
        typer.echo("ERROR: resume timed out after 120s", err=True)
        raise typer.Exit(3)

    typer.echo(proc.stdout[-2000:])
    if proc.returncode != 0:
        typer.echo(f"resume exited {proc.returncode}", err=True)
        typer.echo(proc.stderr[-1000:], err=True)
    else:
        typer.echo("\n✓ resume succeeded")

    if not keep:
        try:
            res.primary_path.unlink()
            typer.echo(f"(removed test session {res.primary_path.name})")
        except OSError:
            pass


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@app.command("sync")
def sync_cmd(
    direction: str = typer.Option("both", "--direction", help="cc-to-codex | codex-to-cc | both"),
    days: int = typer.Option(7, "--days", "-d", help="Only sessions modified within N days"),
    max_bytes: int = typer.Option(25 * 1024 * 1024, "--max-bytes", help="Skip files larger than this"),
) -> None:
    """One-shot batch translate recent sessions in both/given direction(s).

    Idempotent: re-running uses deterministic target UUIDs so unchanged
    sources are skipped and updated ones overwrite the same target file.
    """
    typer.echo(f"sync: direction={direction}, days={days}")
    stats = sync_once(direction=direction, days=days, max_bytes=max_bytes, log=typer.echo)
    typer.echo("")
    typer.echo(
        f"summary: +{stats.translated} translated, "
        f"{stats.skipped_existing} unchanged, "
        f"{stats.skipped_active} active (skipped), "
        f"{stats.skipped_too_big} too-big (skipped), "
        f"{stats.failed} failed"
    )
    if stats.failures:
        typer.echo("\nfailures:")
        for src, err in stats.failures[:10]:
            typer.echo(f"  {src}: {err}")


@app.command("watch")
def watch_cmd(
    interval: int = typer.Option(30, "--interval", "-i", help="Poll interval in seconds"),
    direction: str = typer.Option("both", "--direction"),
    days: int = typer.Option(7, "--days", "-d"),
    max_bytes: int = typer.Option(25 * 1024 * 1024, "--max-bytes"),
) -> None:
    """Daemon mode: keep both sides mirrored. Ctrl-C to stop."""
    watch_loop(interval=interval, direction=direction, days=days, max_bytes=max_bytes, log=typer.echo)


@app.command("install-hook")
def install_hook_cmd(
    target: str = typer.Option("claude-code", "--target", help="claude-code (writes ~/.claude/settings.json Stop hook)"),
    direction: str = typer.Option("cc-to-codex", "--direction"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be added, don't write"),
) -> None:
    """Wire `agent-bridge sync` into your harness's stop/notification hook."""
    if target != "claude-code":
        typer.echo(f"hook target {target!r} not supported; only claude-code for now", err=True)
        raise typer.Exit(2)

    import json
    settings_path = Path.home() / ".claude" / "settings.json"
    cmd = (
        f"{Path(sys.executable).resolve()} -m agent_bridge.cli sync "
        f"--direction {direction} --days 1 >/dev/null 2>&1"
    )
    new_hook = {"type": "command", "command": cmd}

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            typer.echo("settings.json is not valid JSON; refusing to touch.", err=True)
            raise typer.Exit(3)
    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    # Find a matcher="*" group, or create one
    target_group = None
    for g in stop_hooks:
        if g.get("matcher") in ("*", None):
            target_group = g
            break
    if target_group is None:
        target_group = {"matcher": "*", "hooks": []}
        stop_hooks.append(target_group)
    cmds = target_group.setdefault("hooks", [])
    if any(h.get("command") == cmd for h in cmds):
        typer.echo("hook already installed; nothing to do.")
        return
    cmds.append(new_hook)

    if dry_run:
        typer.echo("would write the following to ~/.claude/settings.json:")
        typer.echo(json.dumps(settings, indent=2, ensure_ascii=False))
        return

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))
    typer.echo(f"✓ added Stop hook to {settings_path}")
    typer.echo("Hook fires on every CC turn end; runs sync silently. Test by triggering /clear.")


@app.command("clean")
def clean_cmd(
    direction: str = typer.Option("both", "--direction", help="cc | codex | both"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List files that would be deleted"),
) -> None:
    """Delete every jsonl that agent-bridge previously generated.

    Detection:
      - CC side: jsonl whose first non-empty line is a `custom-title` with
        `[from ...]` prefix
      - Codex side: jsonl whose session_meta.payload.source.custom ==
        "agent-bridge"
    """
    import json
    from .sync import (
        CC_PROJECTS,
        CODEX_HOME,
        _is_agent_bridge_cc,
        _is_agent_bridge_codex,
    )

    targets: list[Path] = []
    if direction in ("cc", "both") and CC_PROJECTS.exists():
        for proj in CC_PROJECTS.iterdir():
            if proj.is_dir():
                for f in proj.glob("*.jsonl"):
                    if _is_agent_bridge_cc(f):
                        targets.append(f)
    if direction in ("codex", "both"):
        sess_root = CODEX_HOME / "sessions"
        if sess_root.exists():
            for f in sess_root.glob("**/rollout-*.jsonl"):
                if _is_agent_bridge_codex(f):
                    targets.append(f)

    if not targets:
        typer.echo("nothing to clean.")
        return
    typer.echo(f"found {len(targets)} agent-bridge-generated file(s):")
    for t in targets:
        typer.echo(f"  {t}")
    if dry_run:
        typer.echo("\n(dry run — no files deleted)")
        return
    for t in targets:
        try:
            t.unlink()
        except OSError as e:
            typer.echo(f"  failed to remove {t}: {e}", err=True)
    typer.echo(f"\n✓ removed {len(targets)} files")


if __name__ == "__main__":
    app()
