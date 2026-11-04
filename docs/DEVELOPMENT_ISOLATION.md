# Isolated development commands on a host with real Basic Memory data

Environment variables alone do not isolate this project's tests: upstream fixtures
deliberately clear configuration overrides while testing default path resolution.
Use `scripts/run_isolated.py` before importing or running the development package
on a machine that also hosts real notes or an installed Basic Memory service.

The Linux launcher uses only the standard library before starting bubblewrap. It
makes the host filesystem read-only and mounts only the source checkout and an
explicit private development directory writable. `/tmp` is private. The existing
default `.basic-memory` directory is covered by a private fallback profile, so
fixtures that clear overrides cannot read or alter the real profile. HOME is not
changed. Children inherit the mount namespace. A missing bubblewrap installation,
overlapping production paths or symlinked private mount sources stop the command.

Choose a state directory outside all configured note roots and outside `/tmp`:

```sh
python3 scripts/run_isolated.py --state /srv/bm-development-state -- \
  uv run pytest tests/repository/test_git_publication.py \
  --import-mode=importlib --no-cov
python3 scripts/run_isolated.py --state /srv/bm-development-state -- \
  just test-unit-sqlite
python3 scripts/run_isolated.py --state /srv/bm-development-state -- \
  just test-int-sqlite
```

The official `just` recipes include importlib collection. Shared Git integration
fixtures live in `tests/git_host_support.py`, which is importable in that mode.
Use directory-based coverage (`--cov=src/basic_memory`); module-name measurement
can import parent service packages too early and trigger a Pydantic initialization
error. A focused coverage run should disable the whole-suite coverage threshold
and check coverage for the changed modules explicitly.

The launcher supplies private configuration, notes, runtime, cache and temporary
paths and removes inherited Basic Memory environment overrides. To choose a test
Redis endpoint or a nested Doctor profile, set its override *inside* the launcher:

```sh
python3 scripts/run_isolated.py --state /srv/bm-development-state -- \
  env BASIC_MEMORY_TEST_REDIS_URL=redis://127.0.0.1:16389/0 \
  just test-int-sqlite
```

This is a filesystem guard for development commands, not an untrusted-code or
network sandbox. External service access remains the caller's responsibility.
Use disposable test services without production data mounts.
