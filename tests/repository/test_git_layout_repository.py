"""Canonical CAS and mirrored-identity guards under interrupted layout changes."""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update

from basic_memory.models import Entity, GitFileRetirement, NoteContent
from basic_memory.repository.git_layout_repository import GitLayoutRepository
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_projection_repository import (
    GitProjectionConflict,
    GitProjectionRepository,
)
from basic_memory.repository.note_content_repository import NoteContentVersionConflict
from basic_memory.services.git_project_projection import project_git_content


@pytest.fixture
async def projected(test_project, session_maker, tmp_path_factory):
    repository = GitProjectRepository.create(
        tmp_path_factory.mktemp("layout-cas") / "project.git",
        project_external_id=UUID(test_project.external_id),
    )
    note_id = uuid4()
    repository.publish(
        repository.prepare(
            transaction_id=uuid4(),
            base_commit=repository.head(),
            files={"a.md": "---\ntitle: A\ntype: note\npermalink: a\n---\n\nPublished\n"},
            note_ids={"a.md": note_id},
        )
    )
    await project_git_content(repository, project_id=test_project.id, session_maker=session_maker)
    return repository, GitLayoutRepository(test_project.id), note_id


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_read", [False, True])
async def test_layout_claim_cannot_delete_newer_accepted_content(
    projected, session_maker, monkeypatch, stale_read
):
    repository, layout, note_id = projected
    async with session_maker() as session:
        expected = await layout.local_notes(session)
    async with session_maker() as session, session.begin():
        await session.execute(
            update(NoteContent)
            .where(NoteContent.entity_id == expected[0].entity_id)
            .values(db_version=2)
        )
    if stale_read:
        monkeypatch.setattr(layout, "local_notes", AsyncMock(return_value=expected))
    with pytest.raises(NoteContentVersionConflict):
        async with session_maker() as session, session.begin():
            await layout.apply(session, expected=expected, target={})
    async with session_maker() as session:
        content = await session.get(NoteContent, expected[0].entity_id)
        assert content is not None and content.db_version == 2
        assert await session.scalar(select(GitFileRetirement.project_id)) is None


@pytest.mark.asyncio
async def test_missing_accepted_content_does_not_grant_retirement(projected, session_maker):
    _, layout, _ = projected
    async with session_maker() as session, session.begin():
        await session.execute(
            delete(NoteContent).where(NoteContent.project_id == layout.project_id)
        )
    async with session_maker() as session:
        expected = await layout.local_notes(session)
    with pytest.raises(GitProjectionConflict, match="without proven"):
        async with session_maker() as session, session.begin():
            await layout.apply(session, expected=expected, target={})


@pytest.mark.asyncio
async def test_disappeared_entity_rolls_back_content_retirement(
    projected, session_maker, monkeypatch
):
    _, layout, _ = projected
    async with session_maker() as session:
        expected = await layout.local_notes(session)
    with pytest.raises(GitProjectionConflict, match="disappeared"):
        async with session_maker() as session, session.begin():
            get = session.get

            async def disappeared(model, key, **kwargs):
                return None if model is Entity else await get(model, key, **kwargs)

            monkeypatch.setattr(session, "get", disappeared)
            await layout.apply(session, expected=expected, target={})
    async with session_maker() as session:
        assert await session.get(NoteContent, expected[0].entity_id) is not None
        assert await session.scalar(select(GitFileRetirement.project_id)) is None


@pytest.mark.asyncio
async def test_direct_projection_identity_guards_are_still_enforced(projected, session_maker):
    _, layout, note_id = projected
    projection = GitProjectionRepository(layout.project_id)
    async with session_maker() as session:
        with pytest.raises(GitProjectionConflict, match="another project or path"):
            await projection.read_basis(session, note_id, "moved.md")
        with pytest.raises(GitProjectionConflict, match="path is already owned"):
            await projection.read_basis(session, uuid4(), "a.md")
    async with session_maker() as session, session.begin():
        await session.execute(
            update(NoteContent)
            .where(NoteContent.project_id == layout.project_id)
            .values(external_id=str(uuid4()))
        )
    async with session_maker() as session:
        with pytest.raises(GitProjectionConflict, match="content identity"):
            await projection.read_basis(session, note_id, "a.md")
