"""Run Linux development commands with private BM state and a read-only host.

This launcher uses only the standard library before filesystem isolation. It does
not import Basic Memory, initialize authentication clients or change HOME. Child processes inherit
the mount namespace even if a pytest fixture removes configuration variables.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def protected_paths() -> list[Path]:
    data = Path.home() / ".basic-memory"
    protected = [data.resolve()]
    config = data / "config.json"
    if config.exists():
        values = json.loads(config.read_text())
        for entry in values.get("projects", {}).values():
            value = entry if isinstance(entry, str) else entry["path"]
            path = Path(value)
            if path.is_absolute():
                protected.append(path.resolve())
    return protected


def require_private(path: Path, protected: list[Path]) -> None:
    if any(path.is_relative_to(root) or root.is_relative_to(path) for root in protected):
        raise ValueError(f"Writable development path overlaps production data: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A command is required after --")
    executable = shutil.which("bwrap")
    if executable is None:
        parser.error("Linux bubblewrap is required; refusing an unprotected fallback")
    repo, state = args.repo.resolve(), args.state.resolve()
    protected = protected_paths()
    require_private(repo, protected)
    require_private(state, protected)
    if not (repo / "src/basic_memory").is_dir() or not (repo / "pyproject.toml").is_file():
        parser.error("--repo must identify the Basic Memory source checkout")
    # Overlaying /tmp must never hide the actual checkout or state mount source.
    if repo.is_relative_to("/tmp") or state.is_relative_to("/tmp"):
        parser.error("--repo and --state must be outside /tmp")
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    state.chmod(0o700)
    for name in ("config", "notes", "tmp", "cache", "fallback-config", "runtime"):
        directory = state / name
        if directory.is_symlink():
            parser.error("Private development directories must not use symlinks")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("BASIC_MEMORY_")}
    environment.update(
        BASIC_MEMORY_CONFIG_DIR=str(state / "config"),
        BASIC_MEMORY_HOME=str(state / "notes"),
        BASIC_MEMORY_ENV="test",
        BASIC_MEMORY_DEVELOPMENT_SANDBOX="1",
        PYTHONPATH=str(repo / "src"),
        UV_CACHE_DIR=str(state / "cache/uv"),
        FASTEMBED_CACHE_PATH=str(state / "cache/fastembed"),
        TMPDIR="/tmp",
        XDG_RUNTIME_DIR=str(state / "runtime"),
    )
    # Upstream fixtures deliberately clear CONFIG_DIR to test home resolution.
    # Cover the existing default directory with private state: those tests must
    # neither read real credentials nor chmod/write the host's default profile.
    fallback_mount = []
    default_config = Path.home() / ".basic-memory"
    if default_config.exists():
        fallback_mount = ["--bind", str(state / "fallback-config"), str(default_config)]
    argv = [
        executable,
        "--die-with-parent",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(repo),
        str(repo),
        "--bind",
        str(state),
        str(state),
        "--bind",
        str(state / "tmp"),
        "/tmp",
        *fallback_mount,
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--chdir",
        str(repo),
        "--",
        *command,
    ]
    return subprocess.run(argv, env=environment, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
