"""The CLI must not write notes behind a configured Git host.

With `git_repositories` configured, every CLI command outside `GIT_HOST_COMMANDS`
is refused before it can touch note roots, the registry or SQL. Commands that stay
available must not publish anything either: they reach the API without a running
Git host, which refuses writes with 503.
"""

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

REPO = Path(__file__).parent.parent.parent


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BASIC_MEMORY_") and key != "PYTEST_CURRENT_TEST"
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        BASIC_MEMORY_HOME=str(home / "basic-memory"),
        BASIC_MEMORY_CONFIG_DIR=str(home / ".basic-memory"),
        BASIC_MEMORY_NO_PROMOS="1",
        BASIC_MEMORY_FORCE_LOCAL="true",
    )
    return subprocess.run(
        ["uv", "run", "bm", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=90,
        cwd=REPO,
    )


@pytest.fixture
def git_home(tmp_path: Path) -> Path:
    """A private profile whose config names a Git repository for its one project."""
    home = tmp_path / "home"
    root = tmp_path / "notes"
    root.mkdir(parents=True)
    config_dir = home / ".basic-memory"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "projects": {"main": {"path": str(root), "mode": "local"}},
                "default_project": "main",
                "auto_update": False,
                "git_repositories": {str(uuid4()): str(tmp_path / "main.git")},
            }
        )
    )
    return home


@pytest.mark.parametrize(
    "args",
    [
        ["project", "add", "extra", "{extra}"],
        ["project", "remove", "main"],
        ["import", "memory-json", "{extra}/in.json"],
        ["reindex"],
        ["reset"],
        ["update"],
    ],
)
def test_commands_outside_the_git_allowlist_are_refused_before_any_effect(
    git_home: Path, tmp_path: Path, args: list[str]
) -> None:
    extra = tmp_path / "extra"
    command = [part.format(extra=extra) for part in args]
    before = (git_home / ".basic-memory" / "config.json").read_text()

    result = _run(git_home, *command)

    # Trigger: a CLI verb that is not one of GIT_HOST_COMMANDS.
    # Why: it could write files, the registry or SQL beside the Git publication path.
    # Outcome: exit 2 with the refusal, and nothing was created or rewritten.
    assert result.returncode == 2, result.stderr
    assert "not connected to Git publication" in result.stderr
    assert not extra.exists()
    assert (git_home / ".basic-memory" / "config.json").read_text() == before
    assert not (git_home / ".basic-memory" / "memory.db").exists()


def test_an_allowed_write_tool_publishes_nothing_without_a_running_git_host(
    git_home: Path, tmp_path: Path
) -> None:
    notes = tmp_path / "notes"

    result = _run(
        git_home, "tool", "write-note", "--title", "Cli", "--folder", "notes", "--content", "x"
    )

    # Why: `tool` stays allowed for reads, but a write has no host to publish through.
    # Outcome: it fails and leaves neither a note file nor a repository behind.
    assert result.returncode != 0, result.stdout
    assert list(notes.rglob("*.md")) == []
    assert not (tmp_path / "main.git").exists()
