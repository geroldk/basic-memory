"""Durable stage independence and fences against late acknowledgements."""

import pytest
from sqlalchemy import delete

from basic_memory.models import Project
from basic_memory.repository.git_progress_repository import GitProgressRepository


@pytest.mark.asyncio
async def test_checkpoints_survive_reopening_and_stale_completions_are_fenced(
    test_project, session_maker
):
    progress = GitProgressRepository(test_project.id, session_maker)
    assert await progress.read() == ()
    first, second = "a" * 40, "b" * 40
    await progress.target(first, vector_key="model-1")
    run = await progress.begin("content", first)
    assert run is not None
    assert await progress.finish("content", first, run)
    old_vector_run = await progress.begin("vectors", first)
    assert old_vector_run is not None
    await progress.target(second, vector_key="model-1")
    assert not await progress.finish("vectors", first, old_vector_run)
    assert await progress.begin("graph", first) is None
    reopened = GitProgressRepository(test_project.id, session_maker)
    rows = {row.stage: row for row in await reopened.read()}
    assert rows["content"].completed_commit == first
    assert rows["vectors"].completed_commit is None
    assert all(row.target_commit == second and row.running_commit is None for row in rows.values())
    vector_run = await reopened.begin("vectors", second)
    assert vector_run is not None
    assert await reopened.finish("vectors", second, vector_run, error="provider unavailable")
    retry = await reopened.begin("vectors", second)
    assert retry is not None
    assert not await reopened.finish("vectors", second, vector_run)
    assert await reopened.finish("vectors", second, retry)
    await reopened.target(second, vector_key="model-2")
    rows = {row.stage: row for row in await reopened.read()}
    assert rows["vectors"].completed_commit is None
    assert rows["vectors"].projection_key == "model-2"
    assert rows["content"].completed_commit == first
    async with session_maker() as session, session.begin():
        await session.execute(delete(Project).where(Project.id == test_project.id))
    assert await progress.read() == ()
