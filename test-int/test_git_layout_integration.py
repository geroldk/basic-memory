"""Published moves/deletes, recovery and private-file protection through real BM."""

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from basic_memory.index.git_projection import GitProjectionPipeline, build_git_projection_pipeline
from basic_memory.index.git_projection_worker import GitProjectionWorker
from basic_memory.models import Entity, GitFileRetirement, NoteContent, Project, Relation
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_projection_repository import GitProjectionConflict
from basic_memory.repository.git_layout_repository import GitLayoutRepository
from basic_memory.services.git_project_projection import project_git_content


@pytest.fixture
def pipeline(tmp_path_factory, test_project, engine_factory, app_config):
    repository = GitProjectRepository.create(
        tmp_path_factory.mktemp("git-layout") / "project.git",
        project_external_id=UUID(test_project.external_id),
    )
    return build_git_projection_pipeline(
        repository, project=test_project, session_maker=engine_factory[1], app_config=app_config
    )


def initial(pipeline: GitProjectionPipeline) -> str:
    files = {
        "notes/Source.md": "---\ntitle: Source\ntype: note\npermalink: test-project/notes/source\n---\n\n- [fact] Layoutsourceprobe\n\n- relates_to [[Target]]\n",
        "notes/Target.md": "---\ntitle: Target\ntype: note\npermalink: test-project/notes/target\n---\n\nRetiredtargetprobe\n",
    }
    return publish(pipeline, files, {path: uuid4() for path in files})


def publish(pipeline: GitProjectionPipeline, files: dict[str, str], ids: dict[str, UUID]) -> str:
    repository = pipeline.repository
    return repository.publish(
        repository.prepare(
            transaction_id=uuid4(), base_commit=repository.head(), files=files, note_ids=ids
        )
    ).commit_id


async def ready(worker: GitProjectionWorker) -> None:
    async with asyncio.timeout(15):
        while not (await worker.status()).caught_up:
            await asyncio.sleep(0.02)


async def settle(pipeline: GitProjectionPipeline) -> None:
    worker = GitProjectionWorker(pipeline, poll_interval=0.02)
    async with worker.run():
        await ready(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["move", "swap", "delete_target", "delete_all", "reuse_path", "coalesce_moves"]
)
async def test_published_layout_change_converges_content_files_graph_and_vectors(
    pipeline, operation
):
    initial(pipeline)
    await settle(pipeline)
    old = pipeline.repository.snapshot()
    async with pipeline.session_maker() as session:
        before = {
            UUID(row.external_id): (row.id, row.file_path)
            for row in await session.scalars(select(Entity))
        }
    files, ids = dict(old.files), dict(old.manifest.note_ids)
    if operation in {"move", "reuse_path", "coalesce_moves"}:
        files["moved/Source.md"] = files.pop("notes/Source.md")
        ids["moved/Source.md"] = ids.pop("notes/Source.md")
    elif operation == "swap":
        files["notes/Source.md"], files["notes/Target.md"] = (
            files["notes/Target.md"],
            files["notes/Source.md"],
        )
        ids["notes/Source.md"], ids["notes/Target.md"] = (
            ids["notes/Target.md"],
            ids["notes/Source.md"],
        )
    elif operation == "delete_target":
        files.pop("notes/Target.md")
        ids.pop("notes/Target.md")
    else:
        files, ids = {}, {}
    publish(pipeline, files, ids)
    if operation == "reuse_path":
        files["notes/Source.md"] = (
            "---\ntitle: Replacement\ntype: note\npermalink: test-project/notes/replacement\n---\n\nReplacementprobe\n"
        )
        ids["notes/Source.md"] = uuid4()
        publish(pipeline, files, ids)
    elif operation == "coalesce_moves":
        files["final/Source.md"] = files.pop("moved/Source.md")
        ids["final/Source.md"] = ids.pop("moved/Source.md")
        publish(pipeline, files, ids)
    await settle(pipeline)
    snapshot = pipeline.repository.snapshot()
    async with pipeline.session_maker() as session:
        entities = {UUID(row.external_id): row for row in await session.scalars(select(Entity))}
        assert set(entities) == set(ids.values())
        assert await session.scalar(select(GitFileRetirement.project_id)) is None
        for path, external_id in ids.items():
            entity = entities[external_id]
            if external_id in before:
                assert entity.id == before[external_id][0]
            assert entity.file_path == path
            content = await session.get(NoteContent, entity.id)
            assert content is not None and content.file_write_status == "synced"
            assert content.markdown_content == snapshot.files[path]
            assert (pipeline.file_service.base_path / path).read_bytes() == snapshot.files[
                path
            ].encode()
        for external_id, (_, old_path) in before.items():
            if old_path not in files:
                assert not (pipeline.file_service.base_path / old_path).exists()
            if external_id not in ids.values():
                assert (
                    await pipeline.search_service.repository.get_entity_chunk_manifest(
                        before[external_id][0]
                    )
                    == []
                )
                assert (
                    await pipeline.search_service.repository.get_entity_physical_chunk_keys(
                        before[external_id][0]
                    )
                    == set()
                )
        if operation == "delete_target":
            source_id = before[old.manifest.note_ids["notes/Source.md"]][0]
            relation = await session.scalar(select(Relation).where(Relation.from_id == source_id))
            assert relation is not None and relation.to_id is None and relation.to_name == "Target"
    for external_id, entity in entities.items():
        rows = await pipeline.search_service.repository.get_entity_search_rows(entity.id)
        assert rows and all(row.file_path == entity.file_path for row in rows)
        manifest = await pipeline.search_service.repository.get_entity_chunk_manifest(entity.id)
        assert manifest and all(row.embedding_status == "ready" for row in manifest)
    if operation == "delete_all":
        assert await pipeline.search_service.repository.count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("delete", [False, True])
async def test_private_old_path_is_preserved_and_retirement_survives_restart(pipeline, delete):
    initial(pipeline)
    await settle(pipeline)
    snapshot = pipeline.repository.snapshot()
    files, ids = dict(snapshot.files), dict(snapshot.manifest.note_ids)
    old_bytes = files.pop("notes/Source.md")
    note_id = ids.pop("notes/Source.md")
    if not delete:
        files["moved/Source.md"], ids["moved/Source.md"] = old_bytes, note_id
    publish(pipeline, files, ids)
    target = pipeline.file_service.base_path / "notes/Source.md"
    target.write_bytes(b"Private editor bytes")
    worker = GitProjectionWorker(pipeline, poll_interval=0.02)
    async with worker.run():
        async with asyncio.timeout(10):
            while not next(
                (row.last_error for row in (await worker.status()).stages if row.stage == "files"),
                None,
            ):
                await asyncio.sleep(0.02)
    assert target.read_bytes() == b"Private editor bytes"
    async with pipeline.session_maker() as session:
        assert await session.scalar(select(GitFileRetirement.project_id)) == pipeline.project_id
    target.write_bytes(old_bytes.encode())
    await settle(pipeline)
    assert not target.exists()


async def child_worker(pipeline, app_config, engine_factory, tmp_path, stage):
    config = tmp_path / "child-config.json"
    config.write_text(app_config.model_dump_json())
    return await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "test-int/fixtures/git_projection_worker_crash.py",
            str(engine_factory[0].url.database),
            str(pipeline.repository.path),
            str(pipeline.project_id),
            str(config),
            stage,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(Path.cwd() / "src"), str(Path.cwd()))),
        },
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("BASIC_MEMORY_TEST_POSTGRES") == "1", reason="SQLite process proof")
@pytest.mark.parametrize("operation", ["move", "delete"])
@pytest.mark.parametrize("stage", ["content", "retirement", "files"])
async def test_layout_process_death_resumes_without_lost_old_file_permission(
    pipeline, app_config, engine_factory, tmp_path, operation, stage
):
    initial(pipeline)
    await settle(pipeline)
    snapshot = pipeline.repository.snapshot()
    files, ids = dict(snapshot.files), dict(snapshot.manifest.note_ids)
    content, note_id = files.pop("notes/Source.md"), ids.pop("notes/Source.md")
    if operation == "move":
        files["moved/Source.md"], ids["moved/Source.md"] = content, note_id
    commit = publish(pipeline, files, ids)
    result = await child_worker(pipeline, app_config, engine_factory, tmp_path, stage)
    assert result.returncode == (76 if stage == "retirement" else 73), result.stderr
    await settle(replace(pipeline, repository=GitProjectRepository(pipeline.repository.path)))
    assert pipeline.repository.head() == commit
    assert not (pipeline.file_service.base_path / "notes/Source.md").exists()
    async with pipeline.session_maker() as session:
        assert await session.scalar(select(GitFileRetirement.project_id)) is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("BASIC_MEMORY_TEST_POSTGRES") == "1", reason="SQLite process proof")
async def test_second_process_owner_is_refused_while_first_keeps_running(
    pipeline, app_config, engine_factory, tmp_path
):
    initial(pipeline)
    worker = GitProjectionWorker(pipeline, poll_interval=0.02)
    async with worker.run():
        await ready(worker)
        result = await child_worker(pipeline, app_config, engine_factory, tmp_path, "content")
        assert result.returncode != 73 and "already running" in result.stderr
        assert (await worker.status()).caught_up


@pytest.mark.asyncio
async def test_unknown_checksum_cannot_authorize_deleting_an_editor_file(pipeline):
    from basic_memory.file_utils import compute_checksum

    initial(pipeline)
    await settle(pipeline)
    snapshot = pipeline.repository.snapshot()
    files, ids = dict(snapshot.files), dict(snapshot.manifest.note_ids)
    files["moved/Source.md"] = files.pop("notes/Source.md")
    ids["moved/Source.md"] = ids.pop("notes/Source.md")
    publish(pipeline, files, ids)
    private = b"Private editor bytes"
    target = pipeline.file_service.base_path / "notes/Source.md"
    target.write_bytes(private)
    async with pipeline.session_maker() as session, session.begin():
        await session.execute(
            update(NoteContent)
            .where(NoteContent.file_path == "notes/Source.md")
            .values(db_checksum=await compute_checksum(private))
        )
    with pytest.raises(GitProjectionConflict, match="proven published ancestry"):
        await project_git_content(
            pipeline.repository,
            project_id=pipeline.project_id,
            session_maker=pipeline.session_maker,
        )
    assert target.read_bytes() == private
    async with pipeline.session_maker() as session:
        assert await session.scalar(select(GitFileRetirement.project_id)) is None


@pytest.mark.asyncio
async def test_rapid_round_trips_preserve_old_permissions_and_current_file(pipeline):
    initial(pipeline)
    await settle(pipeline)
    snapshot = pipeline.repository.snapshot()
    files, ids = dict(snapshot.files), dict(snapshot.manifest.note_ids)
    original = files["notes/Source.md"]
    layout = GitLayoutRepository(pipeline.project_id)
    old_permission = None
    path = "notes/Source.md"
    for step, destination in enumerate(
        ["moved/Source.md", "notes/Source.md", "moved/Source.md", "notes/Source.md"]
    ):
        files[destination] = original if step == 3 else files[path] + f"\nPublished step {step}\n"
        files.pop(path)
        ids[destination] = ids.pop(path)
        publish(pipeline, files, ids)
        result = await project_git_content(
            pipeline.repository,
            project_id=pipeline.project_id,
            session_maker=pipeline.session_maker,
        )
        assert result.notes_moved == 1
        async with pipeline.session_maker() as session:
            pending = await layout.retirements(session)
        if old_permission is None:
            old_permission = pending[0]
        path = destination
    assert old_permission is not None
    async with pipeline.session_maker() as session, session.begin():
        await layout.complete_retirement(session, old_permission)
    async with pipeline.session_maker() as session:
        pending = await layout.retirements(session)
        current = next(row for row in pending if row.file_path == "notes/Source.md")
        assert current.retirement_id != old_permission.retirement_id
        assert len(current.expected_checksums) >= 2
    await settle(pipeline)
    assert (pipeline.file_service.base_path / path).read_bytes() == original.encode()


@pytest.mark.asyncio
async def test_editor_change_during_old_file_cleanup_blocks_completion(pipeline, monkeypatch):
    from basic_memory.index.note_content_materialization import LocalNoteContentStorage

    initial(pipeline)
    await settle(pipeline)
    snapshot = pipeline.repository.snapshot()
    files, ids = dict(snapshot.files), dict(snapshot.manifest.note_ids)
    content = files.pop("notes/Source.md")
    ids.pop("notes/Source.md")
    publish(pipeline, files, ids)
    target = pipeline.file_service.base_path / "notes/Source.md"
    delete = LocalNoteContentStorage.delete_file_if_matches

    async def changed_before_delete(self, path, *, expected_checksum):
        target.write_bytes(b"New private editor bytes")
        return await delete(self, path, expected_checksum=expected_checksum)

    monkeypatch.setattr(LocalNoteContentStorage, "delete_file_if_matches", changed_before_delete)
    worker = GitProjectionWorker(pipeline, poll_interval=0.02)
    async with worker.run():
        async with asyncio.timeout(10):
            while not next(
                (row.last_error for row in (await worker.status()).stages if row.stage == "files"),
                None,
            ):
                await asyncio.sleep(0.02)
    assert target.read_bytes() == b"New private editor bytes"
    monkeypatch.setattr(LocalNoteContentStorage, "delete_file_if_matches", delete)
    target.write_bytes(content.encode())
    await settle(pipeline)


@pytest.mark.asyncio
async def test_git_advance_after_layout_does_not_finish_the_stale_target(pipeline, monkeypatch):
    initial(pipeline)
    apply = GitLayoutRepository.apply
    advanced = False

    async def advance_after_layout(self, session, *, expected, target):
        nonlocal advanced
        result = await apply(self, session, expected=expected, target=target)
        if not advanced:
            advanced = True
            snapshot = pipeline.repository.snapshot()
            files = dict(snapshot.files)
            files["notes/Source.md"] += "\nNew published version\n"
            publish(pipeline, files, snapshot.manifest.note_ids)
        return result

    monkeypatch.setattr(GitLayoutRepository, "apply", advance_after_layout)
    with pytest.raises(GitProjectionConflict, match="advanced during projection"):
        await project_git_content(
            pipeline.repository,
            project_id=pipeline.project_id,
            session_maker=pipeline.session_maker,
        )
    await settle(pipeline)


@pytest.mark.asyncio
async def test_running_worker_refuses_changed_root_registration(pipeline):
    initial(pipeline)
    await settle(pipeline)
    worker = GitProjectionWorker(pipeline, poll_interval=0.02)
    async with worker.run():
        try:
            async with pipeline.session_maker() as session, session.begin():
                await session.execute(
                    update(Project)
                    .where(Project.id == pipeline.project_id)
                    .values(path=str(pipeline.file_service.base_path / "unapproved-root"))
                )
            with pytest.raises(GitProjectionConflict, match="differs from SQL registration"):
                await worker.status()
            with pytest.raises(GitProjectionConflict, match="differs from SQL registration"):
                await pipeline.apply("files", pipeline.repository.head())
        finally:
            async with pipeline.session_maker() as session, session.begin():
                await session.execute(
                    update(Project)
                    .where(Project.id == pipeline.project_id)
                    .values(path=str(pipeline.file_service.base_path))
                )
        worker.notify()
        await ready(worker)
    assert not (pipeline.file_service.base_path / "unapproved-root").exists()
