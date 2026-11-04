"""Recovery CLI skips global initialization and fences child routing."""

import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from basic_memory.cli.main import app
from basic_memory.cli.commands import git
from basic_memory.cli import git_recovery


@pytest.mark.parametrize("returncode,ready", [(1, False), (0, False), (0, True)])
def test_finish_restore_checks_child_and_completion_marker(
    tmp_path, monkeypatch, returncode, ready
):
    target = tmp_path / "restore"
    target.mkdir()
    (target / "RESTORE_INCOMPLETE").touch()
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(tmp_path / "broken-profile"))
    monkeypatch.setenv("BASIC_MEMORY_FORCE_CLOUD", "true")
    home = os.environ.get("HOME")

    def child(argv, *, env, check):
        assert not any(key.startswith("BASIC_MEMORY_") for key in env)
        assert env.get("HOME") == home
        assert argv == [sys.executable, "-m", "basic_memory.cli.git_recovery", str(target), "30.0"]
        if ready:
            (target / "READY.json").write_text("{}")
            (target / "RESTORE_INCOMPLETE").unlink()
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(git.subprocess, "run", child)
    result = CliRunner().invoke(app, ["git", "finish-restore", str(target), "--timeout", "30"])
    assert result.exit_code == (0 if ready and returncode == 0 else 1), result.output
    assert ("Restore complete" if ready else "Restore incomplete") in result.output
    assert not (tmp_path / "broken-profile").exists()


def test_recovery_process_sets_private_paths_before_rebuild(tmp_path, monkeypatch):
    calls = []
    target = tmp_path / "restore"
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(tmp_path / "caller"))
    monkeypatch.setenv("BASIC_MEMORY_HOME", str(tmp_path / "caller-notes"))
    monkeypatch.setattr(sys, "argv", ["git_recovery", str(target), "12"])

    async def rebuilt(destination, *, timeout):
        assert os.environ["BASIC_MEMORY_CONFIG_DIR"] == str(target / "config")
        assert os.environ["BASIC_MEMORY_HOME"] == str(target / "notes")
        calls.append((destination, timeout))

    monkeypatch.setattr("basic_memory.services.git_restore.rebuild_restored_git_instance", rebuilt)
    git_recovery.main()
    assert calls == [(target, 12.0)]


def test_backup_requires_explicit_source_and_keeps_broken_caller_profile_out(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(tmp_path / "caller"))

    class Manifest:
        projects = ["one"]

    def created(config, destination):
        calls.append((config, destination))
        return Manifest()

    monkeypatch.setattr("basic_memory.services.git_backup.create_git_backup", created)
    result = CliRunner().invoke(app, ["git", "backup", str(tmp_path / "backup")])
    assert result.exit_code == 2 and not calls
    result = CliRunner().invoke(
        app,
        ["git", "backup", str(tmp_path / "backup"), "--config", str(tmp_path / "source.json")],
    )
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path / "source.json", tmp_path / "backup")]
    assert "1 projects" in result.output


def test_restore_composes_filesystem_restore_and_rebuild(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "basic_memory.services.git_backup.restore_git_backup",
        lambda backup, destination: calls.append((backup, destination)),
    )
    monkeypatch.setattr(
        git, "finish_restore", lambda target, timeout: calls.append((target, timeout))
    )
    result = CliRunner().invoke(
        app, ["git", "restore", str(tmp_path / "backup"), str(tmp_path / "target")]
    )
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path / "backup", tmp_path / "target"), (tmp_path / "target", 300)]
