"""bm_move across two Git projects: one commit per repository, no silent loss.

Upstream moves a note into another project as a write there followed by a delete here.
On a Git host each half must publish through its own project repository; the move is
deliberately not one atomic commit, because projects are the transaction boundary.
"""

import asyncio
from uuid import UUID

import pytest
from fastmcp import Client

from basic_memory import db
from basic_memory.api.container import resolve_container
from basic_memory.config import ProjectEntry
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.project_repository import ProjectRepository

from tests.git_host_support import host_setup as host_setup


@pytest.fixture
async def second_repository(host_setup, config_manager):
    """A second SQL project with its own Git repository next to `test-project`."""
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
    return second, repository


async def caught_up(*project_ids: str) -> None:
    """Wait until the owned workers have projected every published commit."""
    async with asyncio.timeout(30):
        for project_id in project_ids:
            host = resolve_container().git_host
            assert host is not None
            worker = host.runtime_for(project_id).worker
            while not (await worker.status()).caught_up:
                worker.notify()
                await asyncio.sleep(0.03)


@pytest.mark.asyncio
async def test_a_note_moves_between_git_projects_as_two_separate_commits(
    host_setup, second_repository
):
    from basic_memory.mcp.server import mcp
    import basic_memory.mcp.tools  # noqa: F401

    second, second_repo = second_repository
    first_repo = host_setup.repository
    async with Client(mcp) as client:
        written = await client.call_tool(
            "bm_write",
            {
                "project": "test-project",
                "title": "Mover",
                "directory": "notes",
                "content": "- [fact] Moveprobe\n",
                "output_format": "json",
            },
        )
        assert not written.is_error, written.content
        await caught_up(host_setup.project.external_id)
        source_commit = first_repo.head()
        target_commit = second_repo.head()
        original = first_repo.snapshot().files["notes/Mover.md"]

        moved = await client.call_tool(
            "bm_move",
            {
                "identifier": "notes/Mover.md",
                "destination": "archive/Mover.md",
                "project": "test-project",
                "destination_project": "second",
                "output_format": "json",
            },
            raise_on_error=False,
        )
        assert not moved.is_error, moved.content
        await caught_up(host_setup.project.external_id, second.external_id)

    # Trigger: the move wrote a copy and then deleted the source.
    # Why: each project owns its repository, so both halves publish independently.
    # Outcome: the target gained the note, the source lost it, both heads advanced once.
    target = second_repo.snapshot()
    # The copy keeps the body; its title and frontmatter travel with it. (Upstream copies the
    # frontmatter verbatim, so the permalink keeps the source project's prefix.)
    assert (
        original.split("---", 2)[2].strip()
        == target.files["archive/Mover.md"].split("---", 2)[2].strip()
    )
    assert "title: Mover" in target.files["archive/Mover.md"]
    assert "notes/Mover.md" not in first_repo.snapshot().files
    assert first_repo.head() != source_commit
    assert second_repo.head() != target_commit


@pytest.mark.asyncio
async def test_a_stale_checksum_leaves_both_git_projects_untouched(host_setup, second_repository):
    from basic_memory.mcp.server import mcp
    import basic_memory.mcp.tools  # noqa: F401

    second, second_repo = second_repository
    first_repo = host_setup.repository
    async with Client(mcp) as client:
        written = await client.call_tool(
            "bm_write",
            {
                "project": "test-project",
                "title": "Stale",
                "directory": "notes",
                "content": "- [fact] Staleprobe\n",
                "output_format": "json",
            },
        )
        assert not written.is_error, written.content
        await caught_up(host_setup.project.external_id)
        source_commit = first_repo.head()
        target_commit = second_repo.head()

        moved = await client.call_tool(
            "bm_move",
            {
                "identifier": "notes/Stale.md",
                "destination": "archive/Stale.md",
                "project": "test-project",
                "destination_project": "second",
                "expected_checksum": "0" * 64,
                "output_format": "json",
            },
            raise_on_error=False,
        )
        assert moved.is_error
        await caught_up(host_setup.project.external_id, second.external_id)

    assert first_repo.head() == source_commit
    assert second_repo.head() == target_commit
    assert "notes/Stale.md" in first_repo.snapshot().files
    assert second_repo.snapshot().files == {}


@pytest.mark.asyncio
async def test_a_taken_target_path_keeps_the_source_note(host_setup, second_repository):
    from basic_memory.mcp.server import mcp
    import basic_memory.mcp.tools  # noqa: F401

    second, second_repo = second_repository
    first_repo = host_setup.repository
    async with Client(mcp) as client:
        for project, content in (("test-project", "Sourceprobe"), ("second", "Targetprobe")):
            written = await client.call_tool(
                "bm_write",
                {
                    "project": project,
                    "title": "Same",
                    "directory": "notes",
                    "content": f"- [fact] {content}\n",
                    "output_format": "json",
                },
            )
            assert not written.is_error, written.content
        await caught_up(host_setup.project.external_id, second.external_id)
        source_commit = first_repo.head()
        target_commit = second_repo.head()

        moved = await client.call_tool(
            "bm_move",
            {
                "identifier": "notes/Same.md",
                "destination": "notes/Same.md",
                "project": "test-project",
                "destination_project": "second",
                "output_format": "json",
            },
            raise_on_error=False,
        )
        assert moved.is_error
        await caught_up(host_setup.project.external_id, second.external_id)

    # Why: the move must never overwrite the target nor drop the source.
    assert first_repo.head() == source_commit
    assert second_repo.head() == target_commit
    assert "Sourceprobe" in first_repo.snapshot().files["notes/Same.md"]
    assert "Targetprobe" in second_repo.snapshot().files["notes/Same.md"]
