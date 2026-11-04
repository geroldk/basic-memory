"""Development isolation refuses unsafe writable mounts and inherited BM routing."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_isolated", ROOT / "scripts/run_isolated.py")
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
    return tmp_path


def test_protected_config_covers_legacy_and_current_project_paths(home):
    default = home / ".basic-memory"
    assert launcher.protected_paths() == [default]
    default.mkdir()
    (default / "config.json").write_text(
        json.dumps(
            {
                "projects": {
                    "legacy": str(home / "legacy"),
                    "current": {"path": str(home / "current")},
                    "relative": "relative",
                }
            }
        )
    )
    assert launcher.protected_paths() == [default, home / "legacy", home / "current"]
    with pytest.raises(ValueError, match="overlaps"):
        launcher.require_private(home, [default])
    with pytest.raises(ValueError, match="overlaps"):
        launcher.require_private(default / "nested", [default])
    launcher.require_private(home / "separate", [default])


@pytest.mark.parametrize("fallback", [False, True])
def test_only_checkout_and_private_state_are_writable_and_children_inherit_mounts(
    home, monkeypatch, fallback
):
    if fallback:
        (home / ".basic-memory").mkdir()
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setenv("BASIC_MEMORY_FORCE_CLOUD", "true")
    before = os.environ.get("HOME")
    calls = []

    def run(argv, *, env, check):
        calls.append(argv)
        assert env.get("HOME") == before
        assert "BASIC_MEMORY_FORCE_CLOUD" not in env
        assert env["BASIC_MEMORY_CONFIG_DIR"] == str(state / "config")
        assert env["PYTHONPATH"] == str(ROOT / "src")
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with TemporaryDirectory(prefix=".guard-test-", dir=ROOT) as directory:
        state = Path(directory)
        monkeypatch.setattr(
            sys, "argv", ["run_isolated", "--state", directory, "--", "python", "probe.py"]
        )
        assert launcher.main() == 7
        assert state.stat().st_mode & 0o777 == 0o700
        argv = calls[0]
        assert argv[argv.index("--ro-bind") + 1 :][:2] == ["/", "/"]
        assert "--die-with-parent" in argv and "--unshare-pid" in argv
        assert argv[-3:] == ["--", "python", "probe.py"]
        assert (str(home / ".basic-memory") in argv) is fallback


@pytest.mark.parametrize("refusal", ["command", "bwrap", "repo", "tmp", "symlink"])
def test_unsafe_or_unavailable_launch_never_runs_an_unprotected_child(home, monkeypatch, refusal):
    monkeypatch.setattr(launcher.shutil, "which", lambda _: None if refusal == "bwrap" else "bwrap")
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *_a, **_k: pytest.fail("Unsafe child ran")
    )
    with TemporaryDirectory(prefix=".guard-test-", dir=ROOT) as directory:
        state = Path(directory)
        args = ["run_isolated", "--state", directory]
        if refusal == "repo":
            args += ["--repo", str(home / "empty")]
        if refusal == "tmp":
            args = ["run_isolated", "--state", str(home / "state")]
        if refusal == "symlink":
            (state / "tmp").symlink_to(home, target_is_directory=True)
        if refusal != "command":
            args += ["python"]
        monkeypatch.setattr(sys, "argv", args)
        with pytest.raises(SystemExit) as error:
            launcher.main()
        assert error.value.code == 2
