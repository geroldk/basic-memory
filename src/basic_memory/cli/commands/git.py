"""Explicit offline backups; no initialization of the caller's database."""

import os
from pathlib import Path
import subprocess
import sys

import typer

from basic_memory.cli.app import app

git_app = typer.Typer(help="Offline backup and independent restore of adopted Git projects")
app.add_typer(git_app, name="git")


@git_app.command("backup")
def backup(
    destination: Path = typer.Argument(..., help="New private backup archive outside all sources"),
    config: Path = typer.Option(
        ..., "--config", help="Explicit source config.json; host must stop"
    ),
) -> None:
    from basic_memory.services.git_backup import create_git_backup

    manifest = create_git_backup(config, destination)
    typer.echo(f"Backup complete: {destination} ({len(manifest.projects)} projects)")


@git_app.command("restore")
def restore(
    backup_path: Path = typer.Argument(..., help="Private archive created by git backup"),
    destination: Path = typer.Argument(
        ..., help="New instance directory; existing targets refused"
    ),
    timeout: float = typer.Option(300, min=0.1, help="Index rebuild time limit in seconds"),
) -> None:
    from basic_memory.services.git_backup import restore_git_backup

    restore_git_backup(backup_path, destination)
    finish_restore(destination, timeout)


@git_app.command("finish-restore")
def finish_restore(
    destination: Path = typer.Argument(..., help="Incomplete independent restored instance"),
    timeout: float = typer.Option(300, min=0.1, help="Index rebuild time limit in seconds"),
) -> None:
    # No inherited Basic Memory routing or database override may redirect the
    # rebuild into the caller's installation. HOME and credentials stay unchanged.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("BASIC_MEMORY_")}
    result = subprocess.run(
        [sys.executable, "-m", "basic_memory.cli.git_recovery", str(destination), str(timeout)],
        env=environment,
        check=False,
    )
    if (
        result.returncode != 0
        or not (destination / "READY.json").is_file()
        or (destination / "RESTORE_INCOMPLETE").exists()
    ):
        typer.echo(
            "Restore incomplete; retained target can be retried with git finish-restore.", err=True
        )
        raise typer.Exit(1)
    typer.echo(
        f"Restore complete: {destination} (content, files, graph/full text, vectors rebuilt)"
    )
