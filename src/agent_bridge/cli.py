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
    source_path: Path = typer.Argument(..., help="Source CC jsonl path"),
    prompt: str = typer.Option("Reply with: WORKS", "--prompt", help="Test prompt for codex resume"),
    model: str = typer.Option("gpt-5.5", "--model"),
    keep: bool = typer.Option(False, "--keep", help="Keep the translated jsonl after the test"),
) -> None:
    """Translate then immediately run `codex exec resume` to verify end-to-end."""
    res = translate(
        source_path=source_path,
        source_harness="claude-code",
        target_harness="codex",
        target_dir=None,  # default ~/.codex/sessions
        model_name=model,
    )
    typer.echo(f"translated: {res.session_id}")
    typer.echo(f"file:       {res.primary_path}")

    cmd = [
        "codex", "exec", "resume",
        res.session_id, prompt,
        "--skip-git-repo-check",
        "-o", "/tmp/agent-bridge-smoke.md",
    ]
    typer.echo(f"\nrunning: {' '.join(cmd)}\n")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        typer.echo("ERROR: `codex` not on PATH. Install Codex CLI first.", err=True)
        raise typer.Exit(2)
    except subprocess.TimeoutExpired:
        typer.echo("ERROR: codex exec resume timed out after 120s", err=True)
        raise typer.Exit(3)

    typer.echo(proc.stdout[-2000:])
    if proc.returncode != 0:
        typer.echo(f"codex exited {proc.returncode}", err=True)
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
