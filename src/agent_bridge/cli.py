"""Command-line interface."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

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
    subagent_strategy: str = typer.Option("drop", "--subagent-strategy", help="MVP: drop only"),
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


# Make `agent-resume translate ...` work via the [project.scripts] entry point
@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


if __name__ == "__main__":
    app()
