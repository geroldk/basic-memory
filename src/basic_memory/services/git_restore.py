"""Rebuild an independent restored instance through the ordinary Git host."""

import asyncio
import os
from pathlib import Path

from basic_memory import db
from basic_memory.config import BasicMemoryConfig, resolve_data_dir
from basic_memory.index.git_host import run_git_host
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_projection_repository import register_git_project
from basic_memory.schemas.git_backup import GitBackupManifest
from basic_memory.services.git_backup import sync_directory, write_private


async def rebuild_restored_git_instance(destination: Path, *, timeout: float = 300) -> None:
    """Run only in the restored profile and with no existing process-global DB."""
    destination = destination.resolve()
    config_path = destination / "config/config.json"
    if (
        resolve_data_dir().resolve() != config_path.parent
        or not (destination / "RESTORE_INCOMPLETE").is_file()
        or db._engine is not None
    ):
        raise ValueError("Restore rebuild requires its private, incomplete, unopened instance")
    manifest = GitBackupManifest.model_validate_json(
        (destination / "recovery/manifest.json").read_bytes()
    )
    ready = destination / "READY.json"
    if ready.exists() and GitBackupManifest.model_validate_json(ready.read_bytes()) != manifest:
        raise ValueError("Restore completion evidence differs from its backup")
    config = BasicMemoryConfig.model_validate_json(config_path.read_bytes())
    expected = {
        p.project_id: destination / "repositories" / f"{p.project_id}.git"
        for p in manifest.projects
    }
    if config.git_repositories != expected or {
        name: Path(entry.path) for name, entry in config.projects.items()
    } != {p.name: destination / "notes" / str(p.project_id) for p in manifest.projects}:
        raise ValueError("Restored profile no longer matches its reserved destination")
    config.skip_initialization_sync = False
    try:
        _, factory = await db.get_or_create_db(config.app_database_path, config=config)
        async with db.scoped_session(factory) as session:
            for project in manifest.projects:
                repository = GitProjectRepository(expected[project.project_id])
                snapshot = await asyncio.to_thread(repository.snapshot)
                if snapshot.commit_id != project.published_commit:
                    raise ValueError("Restored published history changed before rebuilding")
                registered = await register_git_project(
                    session,
                    snapshot.manifest,
                    name=project.name,
                    path=destination / "notes" / str(project.project_id),
                )
                registered.is_default = project.name == config.default_project
        async with asyncio.timeout(timeout), run_git_host(config, factory) as host:
            assert host is not None
            while True:
                statuses = [await runtime.worker.status() for runtime in host.runtimes.values()]
                if all(status.caught_up for status in statuses):
                    break
                for runtime in host.runtimes.values():
                    runtime.worker.notify()
                await asyncio.sleep(0.05)
    finally:
        await db.shutdown_db()
    # Enable normal startup only after every projection has completed and the
    # rebuild owner released its locks. No peer is enabled by this operation.
    # A crash can leave both markers. A retry verifies READY and rebuilds safely
    # before finishing the handover; it never gets stuck on an existing marker.
    if not ready.exists():
        write_private(ready, manifest.model_dump_json(indent=2).encode())
    temporary = config_path.with_name(".restored-config.json")
    temporary.unlink(missing_ok=True)
    write_private(temporary, config.model_dump_json(indent=2).encode())
    os.replace(temporary, config_path)
    (destination / "RESTORE_INCOMPLETE").unlink()
    sync_directory(config_path.parent)
    sync_directory(destination)
