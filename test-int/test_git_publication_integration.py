"""BM-006 publication experiment using the real API, SQLite and index pipeline.

Only this fixture installs the publisher at the existing acceptance hook. This is
not runtime configuration or a migration of the local write service.
"""

import asyncio
import os
import subprocess
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from basic_memory.db import DatabaseType, engine_session_factory
from basic_memory.deps import get_engine_factory
from basic_memory.models.base import Base
from basic_memory.repository.git_projection_repository import register_git_project
from basic_memory.repository.search_repository import create_search_repository
from basic_memory.repository.entity_repository import EntityRepository

from basic_memory.index import local_schedulers
from basic_memory.index.note_content_materialization import (
    LocalNoteContentMaterializationProvider,
    drain_pending_materializations,
    recover_stuck_materializations,
)
from basic_memory.mcp.clients.project import ProjectClient
from basic_memory.models import Entity, NoteContent, Observation, Project, Relation
from basic_memory.repository.semantic_chunking import build_vector_chunk_records
from basic_memory.runtime.note_content_responses import RuntimeAcceptedNoteResponse
from basic_memory.repository.git_project_repository import (
    GitProjectRepository,
    GitPublicationConflict,
    PreparedGitPublication,
)
from basic_memory.services.note_content_writes import (
    AcceptedNoteChange,
    NoteContentMutationKind,
    NoteContentMutationService,
)
from basic_memory.services.search_service import SearchService
from basic_memory.services.file_service import FileService
from basic_memory.services.git_project_projection import project_git_content


@dataclass
class PublicationExperiment:
    repository: GitProjectRepository
    base_commit: str
    fault: Literal["before", "after"] | None = None
    prepared: PreparedGitPublication | None = None
    note_external_id: str | None = None


@pytest.fixture
def publication(
    tmp_path_factory: pytest.TempPathFactory, test_project: Project, monkeypatch: pytest.MonkeyPatch
) -> PublicationExperiment:
    repository = GitProjectRepository.create(
        tmp_path_factory.mktemp("git-publication") / "project.git",
        project_external_id=UUID(test_project.external_id),
    )
    assert not repository.path.is_relative_to(Path(test_project.path))
    experiment = PublicationExperiment(repository, repository.head())

    async def publish_hook(
        self: NoteContentMutationService,
        session: AsyncSession,
        *,
        project_external_id: str,
        change: AcceptedNoteChange,
        mutation_kind: NoteContentMutationKind,
        source: str,
    ) -> None:
        assert project_external_id == test_project.external_id
        assert session.in_transaction()
        assert mutation_kind in {"create", "update", "edit"}
        payload = change.payload
        assert isinstance(payload, RuntimeAcceptedNoteResponse)
        # Another connection cannot see these prepared, uncommitted bytes. Git
        # must publish first; derived SQL readers may then lag that publication.
        async with self.session_maker() as reader:
            visible = await reader.scalar(
                select(NoteContent.markdown_content).where(
                    NoteContent.entity_id == payload.entity_id
                )
            )
        assert visible != payload.markdown_content
        snapshot = await asyncio.to_thread(repository.snapshot, experiment.base_commit)
        files = {**snapshot.files, payload.file_path: payload.markdown_content}
        prepared = await asyncio.to_thread(
            repository.prepare,
            transaction_id=uuid4(),
            base_commit=snapshot.commit_id,
            files=files,
            note_ids={**snapshot.manifest.note_ids, payload.file_path: UUID(payload.external_id)},
        )
        experiment.prepared = prepared
        experiment.note_external_id = payload.external_id
        if experiment.fault == "before":
            raise RuntimeError("Interrupted before Git publication")
        result = await asyncio.to_thread(repository.publish, prepared)
        experiment.base_commit = result.commit_id
        if experiment.fault == "after":
            raise RuntimeError("Lost response after Git publication")

    monkeypatch.setattr(NoteContentMutationService, "on_accepted_mutation", publish_hook)
    return experiment


async def assert_indexed_vectors(
    search_service: SearchService, entity_id: int, *, relation_count: int = 0
) -> None:
    repository = search_service.repository
    rows = await repository.get_entity_search_rows(entity_id)
    assert Counter(row.type for row in rows)["observation"] == 1
    assert Counter(row.type for row in rows)["relation"] == relation_count
    expected = {record["chunk_key"] for record in build_vector_chunk_records(rows).records}
    manifest = await repository.get_entity_chunk_manifest(entity_id)
    assert {row.chunk_key for row in manifest} == expected
    assert all(row.embedding_status == "ready" for row in manifest)
    assert expected
    physical = await repository.get_entity_physical_chunk_keys(entity_id)
    if physical is not None:
        assert physical == expected


@pytest.mark.asyncio
async def test_real_api_write_publishes_before_sql_visibility_and_embedding(
    client: AsyncClient,
    test_project: Project,
    search_service: SearchService,
    publication: PublicationExperiment,
    monkeypatch: pytest.MonkeyPatch,
    engine_factory,
) -> None:
    assert await search_service.repository.semantic_effectively_enabled()
    base = f"/v2/projects/{test_project.external_id}"
    target = await client.post(
        base + "/knowledge/write",
        json={"note": {"title": "Target", "directory": "notes", "content": "Stable target."}},
    )
    assert target.status_code == 200, target.text
    # Hold the existing production materializer, rather than blocking an invented
    # callback. A successful write must return while vectors are still outstanding.
    materialize_now = LocalNoteContentMaterializationProvider._materialize_write_now
    accept = LocalNoteContentMaterializationProvider.materialize_write_change
    release = asyncio.Event()

    async def paused_materialize(self, accepted):
        await release.wait()
        return await materialize_now(self, accepted)

    async def production_materialization(self, accepted):
        return await accept(replace(self, test_mode=False), accepted)

    monkeypatch.setattr(
        LocalNoteContentMaterializationProvider, "_materialize_write_now", paused_materialize
    )
    monkeypatch.setattr(
        LocalNoteContentMaterializationProvider,
        "materialize_write_change",
        production_materialization,
    )
    try:
        response = await asyncio.wait_for(
            client.post(
                base + "/knowledge/write",
                json={
                    "note": {
                        "title": "Published",
                        "directory": "notes",
                        "content": "# Published\n\n- [fact] Gitdurabilityprobe\n\n- relates_to [[Target]]\n",
                    }
                },
            ),
            timeout=10,
        )
        assert response.status_code == 200, response.text
        entity = response.json()["entity"]
        snapshot = publication.repository.snapshot()
        assert snapshot.files[entity["file_path"]] == entity["content"]
        assert not release.is_set()
        assert not (Path(test_project.path) / entity["file_path"]).exists()
        read = await client.get(base + f"/knowledge/entities/{entity['external_id']}")
        assert read.status_code == 200, read.text
        assert read.json()["content"] == snapshot.files[entity["file_path"]]
        assert await search_service.repository.get_entity_chunk_manifest(entity["id"]) == []
        release.set()
        await drain_pending_materializations()
        await local_schedulers.drain_background_tasks()
    finally:
        release.set()
        await drain_pending_materializations()

    assert (Path(test_project.path) / entity["file_path"]).read_bytes() == entity[
        "content"
    ].encode()
    _, session_maker = engine_factory
    async with session_maker() as session:
        observations = (
            await session.scalars(select(Observation).where(Observation.entity_id == entity["id"]))
        ).all()
        assert len(observations) == 1
        relations = (
            await session.scalars(select(Relation).where(Relation.from_id == entity["id"]))
        ).all()
        assert len(relations) == 1
        assert relations[0].to_id == target.json()["entity"]["id"]
    found = await client.request(
        "QUERY", base + "/search/", json={"text": "Gitdurabilityprobe", "retrieval_mode": "fts"}
    )
    assert found.status_code == 200, found.text
    assert "Gitdurabilityprobe" in found.text
    await assert_indexed_vectors(search_service, entity["id"], relation_count=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before", "after"])
async def test_accept_failure_recovers_published_content_through_real_bm_index(
    client: AsyncClient,
    test_project: Project,
    search_service: SearchService,
    engine_factory,
    publication: PublicationExperiment,
    fault: Literal["before", "after"],
) -> None:
    publication.fault = fault
    base_commit = publication.repository.head()
    with pytest.raises(RuntimeError, match="Git publication"):
        await client.post(
            f"/v2/projects/{test_project.external_id}/knowledge/write",
            json={
                "note": {
                    "title": "Recovered",
                    "directory": "notes",
                    "content": "# Recovered\n\n- [fact] Recoveryprobe\n",
                }
            },
        )
    prepared = publication.prepared
    assert prepared is not None
    _, session_maker = engine_factory
    async with session_maker() as session:
        assert (
            await session.scalar(select(Entity).where(Entity.project_id == test_project.id)) is None
        )
    assert not (Path(test_project.path) / "notes/Recovered.md").exists()
    reopened = GitProjectRepository(publication.repository.path)
    if fault == "before":
        assert reopened.head() == base_commit
        assert reopened.snapshot().files == {}
        assert reopened.transaction_status(prepared.transaction_id) is None
        return

    snapshot = reopened.snapshot()
    assert snapshot.commit_id == prepared.commit_id
    replay = reopened.publish(prepared)
    assert replay.already_committed
    assert replay.commit_id == snapshot.commit_id
    # Restore the committed UUID and exact SQL bytes before reusing BM's real
    # guarded recovery materializer. No API write or new UUID is replayed here.
    projected = await project_git_content(
        reopened, project_id=test_project.id, session_maker=session_maker
    )
    assert projected.notes_changed == 1
    recovered = await recover_stuck_materializations(
        session_maker=session_maker,
        file_service=FileService(Path(test_project.path)),
        project_id=test_project.id,
    )
    assert recovered.written == 1
    await ProjectClient(client).index(
        test_project.external_id, force_full=True, run_in_background=False
    )
    await local_schedulers.drain_background_tasks()
    async with session_maker() as session:
        entity = await session.scalar(select(Entity).where(Entity.project_id == test_project.id))
        assert entity is not None
        external_id, entity_id = entity.external_id, entity.id
        assert external_id == publication.note_external_id
    assert snapshot.manifest.project_external_id == UUID(test_project.external_id)
    read = await client.get(
        f"/v2/projects/{test_project.external_id}/knowledge/entities/{external_id}"
    )
    assert read.status_code == 200, read.text
    assert read.json()["content"] == snapshot.files["notes/Recovered.md"]
    async with session_maker() as session:
        observations = (
            await session.scalars(select(Observation).where(Observation.entity_id == entity_id))
        ).all()
        assert len(observations) == 1
    await assert_indexed_vectors(search_service, entity_id)
    assert reopened.head() == snapshot.commit_id


@pytest.mark.asyncio
async def test_another_published_commit_rejects_and_rolls_back_a_real_api_edit(
    client: AsyncClient, test_project: Project, publication: PublicationExperiment
) -> None:
    base = f"/v2/projects/{test_project.external_id}/knowledge"
    created = await client.post(
        base + "/write",
        json={"note": {"title": "Guarded", "directory": "notes", "content": "Original"}},
    )
    assert created.status_code == 200, created.text
    entity = created.json()["entity"]
    edited = await client.patch(
        base + f"/entities/{entity['external_id']}",
        json={"operation": "append", "content": "\nAccepted append"},
    )
    assert edited.status_code == 202, edited.text
    accepted = edited.json()["content"]
    original_basis = publication.base_commit
    snapshot = publication.repository.snapshot()
    other = publication.repository.prepare(
        transaction_id=uuid4(),
        base_commit=original_basis,
        files={**snapshot.files, "other.md": "Another published note"},
        note_ids={**snapshot.manifest.note_ids, "other.md": uuid4()},
    )
    publication.repository.publish(other)
    # The experiment retains the previously captured basis; the API's SQLite
    # preparation cannot silently change that basis to the latest Git commit.
    with pytest.raises(GitPublicationConflict):
        await client.patch(
            base + f"/entities/{entity['external_id']}",
            json={"operation": "append", "content": "\nRefused append"},
        )
    read = await client.get(base + f"/entities/{entity['external_id']}")
    assert read.json()["content"] == accepted
    assert publication.repository.snapshot().files[entity["file_path"]] == accepted
    assert (Path(test_project.path) / entity["file_path"]).read_bytes() == accepted.encode()
    assert publication.repository.head() == other.commit_id


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("BASIC_MEMORY_TEST_POSTGRES") == "1", reason="Independent SQLite clone proof"
)
async def test_fresh_clone_restores_portable_ids_with_different_numeric_sql_ids(
    client: AsyncClient,
    app: FastAPI,
    app_config,
    test_project: Project,
    publication: PublicationExperiment,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = f"/v2/projects/{test_project.external_id}/knowledge"
    originals = []
    for title, body in [
        ("Target", "Stable target"),
        ("Source", "# Source\n\n- [fact] Cloneprobe\n\n- relates_to [[Target]]\n"),
    ]:
        response = await client.post(
            base + "/write", json={"note": {"title": title, "directory": "notes", "content": body}}
        )
        assert response.status_code == 200, response.text
        originals.append(response.json()["entity"])
    target_root = tmp_path_factory.mktemp("independent-git-clone")
    clone_path = target_root / "project.git"
    subprocess.run(
        ["git", "clone", "--bare", "--no-local", str(publication.repository.path), str(clone_path)],
        check=True,
        capture_output=True,
    )
    cloned = GitProjectRepository(clone_path)
    snapshot = cloned.snapshot()
    project_root = target_root / "notes"
    project_root.mkdir()
    previous_overrides = dict(app.dependency_overrides)
    try:
        async with engine_session_factory(
            target_root / "clone.db", DatabaseType.FILESYSTEM
        ) as factory:
            engine, session_maker = factory
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text("DROP TABLE IF EXISTS search_index"))
            async with session_maker() as session, session.begin():
                dummy = Project(
                    id=100, name="occupied", path=str(target_root / "occupied"), is_active=True
                )
                session.add(dummy)
                await session.flush()
                session.add(
                    Entity(
                        id=100,
                        project_id=dummy.id,
                        title="Occupied",
                        note_type="note",
                        content_type="text/markdown",
                        file_path="occupied.md",
                    )
                )
                await session.flush()
                restored_project = await register_git_project(
                    session, snapshot.manifest, name=test_project.name, path=project_root
                )
                project_id = restored_project.id
                assert project_id != test_project.id
            search_repository = create_search_repository(
                session_maker, project_id=project_id, app_config=app_config
            )
            await search_repository.init_search_index()
            projected = await project_git_content(
                cloned, project_id=project_id, session_maker=session_maker
            )
            assert projected.notes_changed == projected.notes_total == 2
            recovered = await recover_stuck_materializations(
                session_maker=session_maker,
                file_service=FileService(project_root),
                project_id=project_id,
            )
            assert recovered.written == 2
            app.dependency_overrides[get_engine_factory] = lambda: factory
            await ProjectClient(client).index(
                test_project.external_id, force_full=True, run_in_background=False
            )
            await local_schedulers.drain_background_tasks()
            for original in originals:
                read = await client.get(base + f"/entities/{original['external_id']}")
                assert read.status_code == 200, read.text
                assert read.json()["external_id"] == original["external_id"]
                assert read.json()["id"] != original["id"]
                assert read.json()["content"] == original["content"]
                assert (project_root / original["file_path"]).read_bytes() == original[
                    "content"
                ].encode()
            local_search = SearchService(
                search_repository,
                EntityRepository(project_id),
                FileService(project_root),
                session_maker=session_maker,
            )
            async with session_maker() as session:
                source = await session.scalar(
                    select(Entity).where(Entity.external_id == originals[1]["external_id"])
                )
                assert source is not None
                source_id = source.id
                relation = await session.scalar(
                    select(Relation).where(Relation.from_id == source_id)
                )
                assert relation is not None
                target_id = await session.scalar(
                    select(Entity.id).where(Entity.external_id == originals[0]["external_id"])
                )
                assert relation.to_id == target_id
            await assert_indexed_vectors(local_search, source_id, relation_count=1)
            repeated = await project_git_content(
                cloned, project_id=project_id, session_maker=session_maker
            )
            assert repeated.notes_changed == 0
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
