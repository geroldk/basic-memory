"""Independent restore through real host, SQLite, graph, FTS and vector storage."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID

from filelock import Timeout
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, text

from basic_memory import db
from basic_memory.config import BasicMemoryConfig
from basic_memory.index.git_projection import GitProjectionPipeline
from basic_memory.models import Entity, GitProjectionProgress, Observation, Relation
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_transaction_repository import GitTransactionRepository
from basic_memory.schemas.git_transactions import GitTransactionStart
from basic_memory.services.git_backup import create_git_backup, restore_git_backup
from basic_memory.services.git_restore import rebuild_restored_git_instance
from tests.git_host_support import host_setup as host_setup, ready


@pytest.mark.asyncio
async def test_independent_restore_rebuilds_every_projection_and_keeps_private_drafts(
    host_setup, config_manager, tmp_path, monkeypatch
):
    from basic_memory.api.app import app

    backup, target = tmp_path / "backup.zip", tmp_path / "restored"
    project_id = host_setup.project.external_id
    note_ids = {}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            for title, body in [
                ("Beta", "- [fact] Restorebetaprobe\n"),
                ("Alpha", "- [fact] Restorealphaprobe\n\n- relates_to [[Beta]]\n"),
            ]:
                answer = await client.post(
                    f"/v2/projects/{project_id}/knowledge/write",
                    json={"note": {"title": title, "content": body}},
                )
                assert answer.status_code == 200, answer.text
                note_ids[title] = answer.json()["entity"]["external_id"]
            await ready(client, project_id)
            with pytest.raises(Timeout):
                await asyncio.to_thread(create_git_backup, config_manager.config_file, backup)
    await db.shutdown_db()
    original = host_setup.repository.snapshot()
    records = GitTransactionRepository(host_setup.repository, UUID(project_id))
    draft = records.start(GitTransactionStart())
    await asyncio.to_thread(create_git_backup, config_manager.config_file, backup)
    await asyncio.to_thread(restore_git_backup, backup, target)
    assert not (target / "config/memory.db").exists()
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(target / "config"))
    monkeypatch.setenv("BASIC_MEMORY_HOME", str(target / "notes"))
    await rebuild_restored_git_instance(target, timeout=30)
    assert (target / "READY.json").is_file()
    assert not (target / "RESTORE_INCOMPLETE").exists()
    restored_config = BasicMemoryConfig.model_validate_json(
        (target / "config/config.json").read_bytes()
    )
    assert not restored_config.skip_initialization_sync
    repository = GitProjectRepository(restored_config.git_repositories[UUID(project_id)])
    assert repository.snapshot() == original
    recovered = GitTransactionRepository(repository, UUID(project_id))
    assert recovered.status(draft.transaction_id, draft.transaction_key).state == "open"
    for path, content in original.files.items():
        root = Path(restored_config.projects["test-project"].path)
        assert (root / path).read_bytes() == content.encode()
    # Ordinary API startup must work from the restored configuration and serve
    # the same UUIDs. Its worker also owns physical vector reconciliation.
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            status = await ready(client, project_id)
            assert status["source_commit"] == original.commit_id
            for identity in note_ids.values():
                answer = await client.get(
                    f"/v2/projects/{project_id}/knowledge/entities/{identity}"
                )
                assert answer.status_code == 200, answer.text
            found = await client.request(
                "QUERY",
                f"/v2/projects/{project_id}/search/",
                json={"text": "Restorealphaprobe", "retrieval_mode": "fts"},
            )
            assert found.status_code == 200 and "Restorealphaprobe" in found.text
            _, factory = await db.get_or_create_db(
                restored_config.app_database_path, config=restored_config
            )
            async with factory() as session:
                entities = list(await session.scalars(select(Entity)))
                assert {e.external_id for e in entities} == set(note_ids.values())
                assert await session.scalar(select(Observation.id).limit(1)) is not None
                assert (
                    await session.scalar(select(Relation.id).where(Relation.to_id.is_not(None)))
                    is not None
                )
                progress = list(await session.scalars(select(GitProjectionProgress)))
                assert len(progress) == 4
                assert all(p.completed_commit == original.commit_id for p in progress)
                assert await session.scalar(text("SELECT COUNT(*) FROM search_index")) > 0
            from basic_memory.api.container import resolve_container

            host = resolve_container().git_host
            assert host is not None
            search = host.runtime_for(project_id).pipeline.search_service.repository
            for entity in entities:
                assert await search.get_entity_chunk_manifest(entity.id)
                assert await search.get_entity_physical_chunk_keys(entity.id)


@pytest.mark.asyncio
async def test_failed_rebuild_stays_blocked_and_can_resume(
    host_setup, config_manager, tmp_path, monkeypatch
):
    from basic_memory.api.app import app

    async with app.router.lifespan_context(app):
        pass
    await db.shutdown_db()
    backup, target = tmp_path / "backup.zip", tmp_path / "restored"
    create_git_backup(config_manager.config_file, backup)
    restore_git_backup(backup, target)
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(target / "config"))
    apply = GitProjectionPipeline.apply

    async def broken(self, stage, commit_id, *, graph_base_commit=None):
        if stage == "graph":
            raise OSError("Simulated index failure")
        await apply(self, stage, commit_id, graph_base_commit=graph_base_commit)

    monkeypatch.setattr(GitProjectionPipeline, "apply", broken)
    with pytest.raises(TimeoutError):
        await rebuild_restored_git_instance(target, timeout=0.3)
    assert db._engine is None
    assert (target / "RESTORE_INCOMPLETE").is_file()
    assert not (target / "READY.json").exists()
    config = BasicMemoryConfig.model_validate_json((target / "config/config.json").read_bytes())
    assert config.skip_initialization_sync
    monkeypatch.setattr(GitProjectionPipeline, "apply", apply)
    await rebuild_restored_git_instance(target, timeout=30)
    assert (target / "READY.json").exists()


@pytest.mark.asyncio
async def test_rebuild_refuses_caller_database_and_modified_profile(tmp_path, monkeypatch):
    target = tmp_path / "restored"
    target.mkdir()
    with pytest.raises(ValueError, match="private, incomplete"):
        await rebuild_restored_git_instance(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["profile", "head", "completion"])
async def test_modified_restore_cannot_claim_completion(
    host_setup, config_manager, tmp_path, monkeypatch, fault
):
    from basic_memory.api.app import app

    async with app.router.lifespan_context(app):
        pass
    await db.shutdown_db()
    backup, target = tmp_path / "backup.zip", tmp_path / "restored"
    create_git_backup(config_manager.config_file, backup)
    restore_git_backup(backup, target)
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(target / "config"))
    if fault == "profile":
        path = target / "config/config.json"
        config = json.loads(path.read_bytes())
        config["projects"]["test-project"]["path"] = str(host_setup.repository.path)
        path.write_text(json.dumps(config))
    elif fault == "completion":
        manifest = json.loads((target / "recovery/manifest.json").read_bytes())
        manifest["producer_version"] = "different"
        (target / "READY.json").write_text(json.dumps(manifest))
    else:
        repository = GitProjectRepository(
            target / "repositories" / f"{host_setup.project.external_id}.git"
        )
        prepared = repository.prepare(
            transaction_id=UUID(host_setup.project.external_id),
            base_commit=repository.head(),
            files={},
            note_ids={},
        )
        # Preserve a well-formed, distinct published commit with the same empty tree.
        commit = (
            repository._git(
                "commit-tree",
                repository._git("rev-parse", "main^{tree}").strip().decode(),
                "-p",
                prepared.base_commit,
                input_bytes=b"Unexpected publication\n",
            )
            .strip()
            .decode()
        )
        repository._git("update-ref", "refs/heads/main", commit)
    with pytest.raises(ValueError):
        await rebuild_restored_git_instance(target)
    assert db._engine is None
    assert (target / "RESTORE_INCOMPLETE").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["handover_before_ready", "handover_after_ready", "handover_after_config"]
)
async def test_process_death_during_restore_handover_is_resumable(
    host_setup, config_manager, tmp_path, monkeypatch, phase
):
    from basic_memory.api.app import app

    async with app.router.lifespan_context(app):
        pass
    await db.shutdown_db()
    backup, target = tmp_path / "backup.zip", tmp_path / "restored"
    create_git_backup(config_manager.config_file, backup)
    restore_git_backup(backup, target)
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "test-int/fixtures/git_backup_crash.py", phase, str(backup), str(target)],
        capture_output=True,
        timeout=40,
    )
    assert result.returncode == 84, result.stderr
    assert (target / "RESTORE_INCOMPLETE").exists()
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(target / "config"))
    await rebuild_restored_git_instance(target, timeout=30)
    assert (target / "READY.json").exists()
    assert not (target / "RESTORE_INCOMPLETE").exists()
