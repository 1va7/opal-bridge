"""Command-line interface."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

from .adapters.claude_code.listing import fmt_mtime, fmt_size, list_sessions
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


if __name__ == "__main__":
    app()
