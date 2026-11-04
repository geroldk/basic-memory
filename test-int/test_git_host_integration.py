"""Normal API/MCP lifespans, real SQLite/Git and guarded legacy ingress."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from mcp.types import TextContent
from sqlalchemy import select

from basic_memory import db
from basic_memory.api.container import resolve_container
from basic_memory.config import BasicMemoryConfig, ProjectEntry
from basic_memory.index.git_projection import GitProjectionPipeline
from basic_memory.models import Entity, Observation, Project, Relation
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.project_repository import ProjectRepository
from basic_memory.repository.git_projection_repository import GitProjectionConflict


@dataclass(frozen=True)
class HostSetup:
    config: BasicMemoryConfig
    project: Project
    repository: GitProjectRepository


@pytest.fixture
async def host_setup(app_config, config_manager, tmp_path, monkeypatch):
    await db.shutdown_db()
    root = tmp_path / "notes"
    root.mkdir()
    app_config.projects = {"test-project": ProjectEntry(path=str(root))}
    app_config.index_changes = True
    # Exercise real local watch/follow-up decisions, not the test-mode shortcuts.
    monkeypatch.setattr(BasicMemoryConfig, "is_test_env", property(lambda self: False))
    config_manager.save_config(app_config)
    _, factory = await db.get_or_create_db(app_config.database_path)
    async with db.scoped_session(factory) as session:
        project = await ProjectRepository().create(
            session,
            {
                "name": "test-project",
                "path": str(root),
                "is_active": True,
                "is_default": True,
            },
        )
    repository = GitProjectRepository.create(
        tmp_path / "project.git", project_external_id=UUID(project.external_id)
    )
    app_config.git_repositories = {UUID(project.external_id): repository.path}
    config_manager.save_config(app_config)
    try:
        yield HostSetup(app_config, project, repository)
    finally:
        await db.shutdown_db()


async def ready(client, project_id):
    async with asyncio.timeout(30):
        while True:
            response = await client.get(f"/v2/projects/{project_id}/git/status")
            assert response.status_code == 200, response.text
            if response.json()["caught_up"]:
                return response.json()
            host = resolve_container().git_host
            assert host is not None
            host.runtime_for(project_id).worker.notify()
            await asyncio.sleep(0.03)


@pytest.mark.asyncio
async def test_normal_api_startup_uses_git_and_reports_read_graph_search_progress(host_setup):
    from basic_memory.api.app import app

    project = host_setup.project
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            assert resolve_container().git_host is not None
            response = await client.post(
                f"/v2/projects/{project.external_id}/knowledge/write",
                json={
                    "note": {
                        "title": "Host",
                        "directory": "notes",
                        "content": "- [fact] Hostprobe\n",
                    }
                },
            )
            assert response.status_code == 200, response.text
            note = response.json()["entity"]
            snapshot = host_setup.repository.snapshot()
            assert snapshot.files[note["file_path"]] == note["content"]
            read = await client.get(
                f"/v2/projects/{project.external_id}/knowledge/entities/{note['external_id']}"
            )
            assert read.status_code == 200 and read.json()["content"] == note["content"]
            status = await ready(client, project.external_id)
            assert status["source_commit"] == snapshot.commit_id
            assert status["read_consistency"] == "relaxed"
            assert {row["stage"] for row in status["stages"]} == {
                "content",
                "files",
                "graph",
                "vectors",
            }
            found = await client.request(
                "QUERY",
                f"/v2/projects/{project.external_id}/search/",
                json={"text": "Hostprobe", "retrieval_mode": "fts"},
            )
            assert found.status_code == 200 and "Hostprobe" in found.text
            base = f"/v2/projects/{project.external_id}/knowledge/entities/{note['external_id']}"
            updated = await client.put(
                base,
                json={
                    "title": "Host",
                    "directory": "notes",
                    "content": "- [fact] Updatedhostprobe\n",
                },
            )
            assert updated.status_code in {200, 202}, updated.text
            edited = await client.patch(
                base, json={"operation": "append", "content": "\nAppendhostprobe\n"}
            )
            assert edited.status_code == 202, edited.text
            await ready(client, project.external_id)
            moved = await client.put(base + "/move", json={"destination_path": "moved/Host.md"})
            assert moved.status_code in {200, 202}, moved.text
            await ready(client, project.external_id)
            assert host_setup.repository.snapshot().manifest.note_ids == {
                "moved/Host.md": UUID(note["external_id"])
            }
            deleted = await client.delete(base)
            assert deleted.status_code == 202, deleted.text
            await ready(client, project.external_id)
            snapshot = host_setup.repository.snapshot()
            assert snapshot.files == {}
    assert host_setup.repository.head() == snapshot.commit_id


@pytest.mark.asyncio
async def test_git_config_without_running_lifespan_refuses_write(host_setup):
    from basic_memory.api.app import app

    base = host_setup.repository.head()
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            f"/v2/projects/{host_setup.project.external_id}/knowledge/write",
            json={"note": {"title": "Refused", "content": "Private"}},
        )
        assert response.status_code == 503, response.text
    assert host_setup.repository.head() == base


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/knowledge/index-file", {"file_path": "Private.md"}),
        ("POST", "/index", {}),
        ("POST", "/search/reindex", {}),
        ("POST", "/knowledge/move-directory", {"source": "notes", "destination": "moved"}),
        ("POST", "/knowledge/delete-directory", {"directory": "notes"}),
        ("PATCH", "", {"path": "/tmp/elsewhere"}),
        ("DELETE", "", {}),
        ("PUT", "/default", {}),
        ("POST", "/schema/infer", {"note_type": "note"}),
        ("POST", "/schema/validate", {}),
        ("POST", "/import/memory-json", {}),
    ],
)
async def test_unconnected_api_mutations_are_refused_before_acceptance(
    host_setup, method, path, payload
):
    from basic_memory.api.app import app

    base = host_setup.repository.head()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.request(
                method, f"/v2/projects/{host_setup.project.external_id}" + path, json=payload
            )
            assert response.status_code == 403, response.text
    assert host_setup.repository.head() == base
    assert host_setup.repository.snapshot().files == {}


@pytest.mark.asyncio
async def test_api_writes_and_deletes_return_while_owned_indexes_are_blocked(
    host_setup, monkeypatch
):
    from basic_memory.api.app import app
    from basic_memory.index.local_schedulers import LocalRelationResolutionScheduler

    apply = GitProjectionPipeline.apply
    release = asyncio.Event()

    async def blocked(self, stage, commit_id, *, graph_base_commit=None):
        if stage in {"graph", "vectors"}:
            await release.wait()
        await apply(self, stage, commit_id, graph_base_commit=graph_base_commit)

    def forbidden(self, *, project_id):
        pytest.fail("Legacy relation jobs must not run in the Git host")

    monkeypatch.setattr(GitProjectionPipeline, "apply", blocked)
    monkeypatch.setattr(LocalRelationResolutionScheduler, "schedule_relation_resolution", forbidden)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            base = f"/v2/projects/{host_setup.project.external_id}/knowledge"
            response = await asyncio.wait_for(
                client.post(
                    base + "/entities", json={"title": "Async", "content": "- [fact] Asyncprobe\n"}
                ),
                5,
            )
            assert response.status_code == 202, response.text
            note = response.json()
            _, factory = await db.get_or_create_db(host_setup.config.database_path)
            async with factory() as session:
                assert await session.scalar(select(Observation.id)) is None
                assert await session.scalar(select(Relation.id)) is None
            created = host_setup.repository.head()
            response = await asyncio.wait_for(
                client.delete(base + f"/entities/{note['external_id']}"), 5
            )
            assert response.status_code == 202, response.text
            assert host_setup.repository.head() != created
            assert not release.is_set()
            release.set()
            await ready(client, host_setup.project.external_id)
            _, factory = await db.get_or_create_db(host_setup.config.database_path)
            async with factory() as session:
                assert await session.scalar(select(Entity.id)) is None


@pytest.mark.asyncio
async def test_normal_mcp_tools_publish_read_and_report_git_status(host_setup):
    from basic_memory.mcp.server import mcp
    import basic_memory.mcp.tools  # noqa: F401

    async with Client(mcp) as client:
        listing = await client.call_tool("bm_project", {"command": "list", "output_format": "json"})
        assert "test-project" in str(listing.content)
        result = await client.call_tool(
            "bm_write",
            {
                "project": "test-project",
                "title": "MCP",
                "directory": "notes",
                "content": "- [fact] Mcphostprobe\n",
                "output_format": "json",
            },
        )
        assert not result.is_error
        assert "notes/MCP.md" in host_setup.repository.snapshot().files
        read = await client.call_tool(
            "bm_read", {"project": "test-project", "identifier": "MCP", "output_format": "json"}
        )
        assert "Mcphostprobe" in str(read.content)
        status = await client.call_tool(
            "bm_project", {"command": "status", "project": "test-project", "output_format": "json"}
        )
        block = status.content[0]
        assert isinstance(block, TextContent)
        payload = json.loads(block.text)
        assert payload["source_commit"] == host_setup.repository.head()
        assert payload["read_consistency"] == "relaxed"


@pytest.mark.asyncio
async def test_startup_restores_published_sql_before_admission_even_when_graph_is_blocked(
    host_setup, monkeypatch
):
    from basic_memory.api.app import app

    text = "---\ntitle: Recovered\ntype: note\npermalink: test-project/recovered\n---\n\n- [fact] Recoveredhostprobe\n"
    identity = uuid4()
    commit = host_setup.repository.publish(
        host_setup.repository.prepare(
            transaction_id=uuid4(),
            base_commit=host_setup.repository.head(),
            files={"Recovered.md": text},
            note_ids={"Recovered.md": identity},
        )
    ).commit_id
    apply = GitProjectionPipeline.apply
    release = asyncio.Event()

    async def blocked(self, stage, commit_id, *, graph_base_commit=None):
        if stage in {"graph", "vectors"}:
            await release.wait()
        await apply(self, stage, commit_id, graph_base_commit=graph_base_commit)

    monkeypatch.setattr(GitProjectionPipeline, "apply", blocked)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.get(
                f"/v2/projects/{host_setup.project.external_id}/knowledge/entities/{identity}"
            )
            assert response.status_code == 200 and response.json()["content"] == text
            status = await client.get(f"/v2/projects/{host_setup.project.external_id}/git/status")
            assert status.json()["source_commit"] == commit
            assert not status.json()["caught_up"]
            release.set()
            await ready(client, host_setup.project.external_id)


@pytest.mark.asyncio
async def test_existing_indexed_delete_keeps_sql_cleanup_bounded_and_worker_owned(
    host_setup, monkeypatch
):
    from basic_memory.api.app import app

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            prefix = f"/v2/projects/{host_setup.project.external_id}"
            response = await client.post(
                prefix + "/knowledge/write",
                json={"note": {"title": "Indexed", "content": "- [fact] Deletehostprobe\n"}},
            )
            note = response.json()["entity"]
            await ready(client, host_setup.project.external_id)
            host = resolve_container().git_host
            assert host is not None
            search = host.runtime_for(
                host_setup.project.external_id
            ).pipeline.search_service.repository
            assert await search.get_entity_chunk_manifest(note["id"])
            assert await search.get_entity_physical_chunk_keys(note["id"])
            apply = GitProjectionPipeline.apply
            release = asyncio.Event()

            async def blocked(self, stage, commit_id, *, graph_base_commit=None):
                if stage in {"graph", "vectors"}:
                    await release.wait()
                await apply(self, stage, commit_id, graph_base_commit=graph_base_commit)

            monkeypatch.setattr(GitProjectionPipeline, "apply", blocked)
            deleted = await asyncio.wait_for(
                client.delete(prefix + f"/knowledge/entities/{note['external_id']}"), 5
            )
            assert deleted.status_code == 202, deleted.text
            assert not await search.get_entity_search_rows(note["id"])
            assert not await search.get_entity_chunk_manifest(note["id"])
            assert not await search.get_entity_physical_chunk_keys(note["id"])
            release.set()
            await ready(client, host_setup.project.external_id)


@pytest.mark.asyncio
async def test_second_api_or_mcp_owner_cannot_close_the_existing_hosts_engine(host_setup):
    from basic_memory.api.app import app
    from basic_memory.mcp.server import lifespan, mcp

    async with app.router.lifespan_context(app):
        owner = resolve_container()
        engine = db._engine
        with pytest.raises(GitProjectionConflict, match="already running"):
            async with app.router.lifespan_context(app):
                pytest.fail("Second owner admitted")
        assert resolve_container() is owner and db._engine is engine
        with pytest.raises(GitProjectionConflict, match="already running"):
            async with lifespan(mcp):
                pytest.fail("Second MCP owner admitted")
        assert resolve_container() is owner and db._engine is engine
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            await ready(client, host_setup.project.external_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "extra_sql",
        "missing_mapping",
        "extra_config",
        "cloud",
        "inactive",
        "root",
        "relative_repo",
        "postgres",
        "stateless",
        "foreign_manifest",
    ],
)
async def test_host_refuses_uncontrolled_topology_without_deleting_registry_rows(host_setup, fault):
    from basic_memory.config import DatabaseBackend, ProjectMode
    from basic_memory.index.git_host import run_git_host
    from basic_memory.repository.git_projection_repository import GitProjectionConflict
    from sqlalchemy import update

    config = host_setup.config.model_copy(deep=True)
    _, factory = await db.get_or_create_db(config.database_path)
    if fault == "extra_sql":
        async with db.scoped_session(factory) as session:
            await ProjectRepository().create(
                session,
                {
                    "name": "private",
                    "path": str(host_setup.repository.path.parent / "private"),
                    "is_active": True,
                },
            )
    elif fault == "missing_mapping":
        config.git_repositories = {uuid4(): host_setup.repository.path}
    elif fault == "extra_config":
        config.projects["extra"] = ProjectEntry(
            path=str(host_setup.repository.path.parent / "extra")
        )
    elif fault == "cloud":
        config.projects["test-project"].mode = ProjectMode.CLOUD
    elif fault == "inactive":
        async with db.scoped_session(factory) as session:
            await session.execute(update(Project).values(is_active=False))
    elif fault == "root":
        config.projects["test-project"].path += "/mismatch"
    elif fault == "relative_repo":
        config.git_repositories[UUID(host_setup.project.external_id)] = Path("relative.git")
    elif fault == "postgres":
        config.database_backend = DatabaseBackend.POSTGRES
    elif fault == "stateless":
        config.skip_initialization_sync = True
    else:
        foreign = GitProjectRepository.create(
            host_setup.repository.path.parent / "foreign.git", project_external_id=uuid4()
        )
        config.git_repositories[UUID(host_setup.project.external_id)] = foreign.path
    with pytest.raises(GitProjectionConflict):
        async with run_git_host(config, factory):
            pytest.fail("Invalid host admitted")
    async with factory() as session:
        assert (
            await session.scalar(select(Project.id).where(Project.id == host_setup.project.id))
            is not None
        )


@pytest.mark.asyncio
async def test_empty_git_configuration_keeps_legacy_host_selection(host_setup):
    from basic_memory.index.git_host import run_git_host

    config = host_setup.config.model_copy(update={"git_repositories": {}})
    _, factory = await db.get_or_create_db(config.database_path)
    async with run_git_host(config, factory) as host:
        assert host is None


@pytest.mark.asyncio
async def test_two_projects_publish_to_separate_repositories(host_setup, config_manager):
    from basic_memory.api.app import app

    root = host_setup.repository.path.parent / "second"
    root.mkdir()
    config = host_setup.config
    config.projects["second"] = ProjectEntry(path=str(root))
    _, factory = await db.get_or_create_db(config.database_path)
    async with db.scoped_session(factory) as session:
        second = await ProjectRepository().create(
            session, {"name": "second", "path": str(root), "is_active": True}
        )
    repository = GitProjectRepository.create(
        root.parent / "second.git", project_external_id=UUID(second.external_id)
    )
    config.git_repositories[UUID(second.external_id)] = repository.path
    config_manager.save_config(config)
    original = host_setup.repository.head()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.post(
                f"/v2/projects/{second.external_id}/knowledge/write",
                json={"note": {"title": "Second", "content": "Independent"}},
            )
            assert response.status_code == 200, response.text
            await ready(client, second.external_id)
            assert host_setup.repository.head() == original
            assert "Second.md" in repository.snapshot().files


@pytest.mark.asyncio
async def test_normal_startup_does_not_reconcile_away_an_unconfigured_sql_project(host_setup):
    from basic_memory.api.app import app

    _, factory = await db.get_or_create_db(host_setup.config.database_path)
    async with db.scoped_session(factory) as session:
        extra = await ProjectRepository().create(
            session, {"name": "private", "path": str(host_setup.repository.path.parent / "private")}
        )
    with pytest.raises(GitProjectionConflict, match="Every configured"):
        async with app.router.lifespan_context(app):
            pytest.fail("Uncontrolled SQL project admitted")
    async with factory() as session:
        assert await session.scalar(select(Project.id).where(Project.id == extra.id)) == extra.id


@pytest.mark.asyncio
async def test_git_mcp_stays_local_with_cloud_credentials_and_exposes_progress(
    host_setup, config_manager, monkeypatch
):
    from basic_memory.api.app import app
    from basic_memory.mcp.server import mcp
    from basic_memory.mcp.tools import project_management

    async def forbidden(**kwargs):
        pytest.fail("Git project discovery must not contact Cloud")

    monkeypatch.setattr(project_management, "ensure_workspace_project_index", forbidden)
    host_setup.config.cloud_api_key = "test-only-unused-cloud-key"
    config_manager.save_config(host_setup.config)
    async with Client(mcp) as client:
        listing = await client.call_tool("bm_project", {"command": "list", "output_format": "json"})
        assert "test-project" in str(listing.content)
        assert '"sync_supported":false' in str(listing.content).replace(" ", "").replace("\\", "")
        missing = await client.call_tool("bm_project", {"command": "status"}, raise_on_error=False)
        assert missing.is_error and "status needs" in str(missing.content)
        status = await client.call_tool(
            "bm_project", {"command": "status", "project": "test-project"}
        )
        assert "Reads may mix published revisions" in str(status.content)
        result = await client.call_tool(
            "bm_write",
            {
                "project": "test-project",
                "title": "Local",
                "directory": "notes",
                "content": "Localhostprobe",
            },
        )
        assert not result.is_error, result.content
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as api:
            await ready(api, host_setup.project.external_id)
        found = await client.call_tool(
            "bm_search",
            {
                "query": "Localhostprobe",
                "project": "test-project",
                "mode": "text",
                "output_format": "json",
            },
        )
        assert not found.is_error and "Localhostprobe" in str(found.content)


@pytest.mark.asyncio
async def test_git_followups_and_unowned_status_fail_explicitly(host_setup):
    from basic_memory.api.app import app
    from basic_memory.deps.git import get_git_runtime
    from basic_memory.index.git_followups import GitNoteFollowups
    from basic_memory.runtime.note_content import RuntimeAcceptedNoteChange
    from fastapi import HTTPException

    async with app.router.lifespan_context(app):
        host = resolve_container().git_host
        assert host is not None
        runtime = host.runtime_for(host_setup.project.external_id)
        with pytest.raises(GitProjectionConflict, match="not owned"):
            host.runtime_for(str(uuid4()))
        with pytest.raises(HTTPException) as error:
            await get_git_runtime(-1, host)
        assert error.value.status_code == 404
        followups = GitNoteFollowups(runtime)
        accepted = RuntimeAcceptedNoteChange(status_code=202, payload={})
        assert await followups.materialize_delete_change(accepted) is accepted
        with pytest.raises(GitProjectionConflict, match="legacy materialization"):
            await followups.materialize_write_change(
                RuntimeAcceptedNoteChange(
                    status_code=202, payload={}, relation_cleanup_entity_ids=frozenset({1})
                )
            )
        with pytest.raises(GitProjectionConflict, match="different Git project"):
            followups.schedule_relation_resolution(project_id=-1)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            status = await client.get(f"/v2/projects/{uuid4()}/git/status")
            assert status.status_code == 404


@pytest.mark.asyncio
async def test_legacy_file_indexer_is_refused_even_if_called_directly(host_setup):
    from basic_memory.services.initialization import initialize_file_indexing

    with pytest.raises(GitProjectionConflict, match="Legacy file indexing"):
        await initialize_file_indexing(host_setup.config)


@pytest.mark.asyncio
async def test_mcp_progress_reports_worker_failure_without_claiming_completion(
    host_setup, monkeypatch
):
    from basic_memory.mcp.server import mcp

    apply = GitProjectionPipeline.apply

    async def broken(self, stage, commit_id, *, graph_base_commit=None):
        if stage == "graph":
            raise RuntimeError("Host graph probe failed")
        await apply(self, stage, commit_id, graph_base_commit=graph_base_commit)

    monkeypatch.setattr(GitProjectionPipeline, "apply", broken)
    async with Client(mcp) as client:
        host = resolve_container().git_host
        assert host is not None
        worker = host.runtime_for(host_setup.project.external_id).worker
        async with asyncio.timeout(10):
            while not any(row.last_error for row in (await worker.status()).stages):
                worker.notify()
                await asyncio.sleep(0.03)
        status = await client.call_tool(
            "bm_project", {"command": "status", "project": "test-project"}
        )
        assert "Indexes catching up" in str(status.content)
        assert "Host graph probe failed" in str(status.content)
        assert "pending" in str(status.content)
