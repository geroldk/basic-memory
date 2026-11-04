"""Durable stage independence and fences against late acknowledgements."""

import pytest
from sqlalchemy import delete

from basic_memory.models import Project
from basic_memory.repository.git_progress_repository import (
    GitProgressRepository,
    invalidate_git_graphs,
)


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


@pytest.mark.asyncio
async def test_partial_graph_cannot_reuse_a_preceding_completed_snapshot(
    test_project, session_maker
):
    progress = GitProgressRepository(test_project.id, session_maker)
    first, second, third = "a" * 40, "b" * 40, "c" * 40
    await progress.target(first, vector_key="model")
    run = await progress.begin("graph", first)
    assert run is not None and await progress.finish("graph", first, run)
    await progress.target(second, vector_key="model")
    rows = {row.stage: row for row in await progress.read()}
    assert rows["graph"].reusable_commit == first
    partial = await progress.begin("graph", second, reusable_commit=first)
    assert partial is not None
    await progress.target(third, vector_key="model")
    rows = {row.stage: row for row in await progress.read()}
    assert rows["graph"].completed_commit == first
    assert rows["graph"].reusable_commit is None
    assert not await progress.finish("graph", second, partial)
    assert await progress.begin("graph", third, reusable_commit=first) is None
    complete = await progress.begin("graph", third)
    assert complete is not None and await progress.finish("graph", third, complete)
    assert (
        next(row for row in await progress.read() if row.stage == "graph").reusable_commit == third
    )


@pytest.mark.asyncio
async def test_structural_sql_change_fences_all_project_graph_and_vector_acknowledgements(
    test_project, session_maker
):
    async with session_maker() as session, session.begin():
        other = Project(name="other", path="/tmp/other-git-graph", is_active=True)
        session.add(other)
    repositories = (
        GitProgressRepository(test_project.id, session_maker),
        GitProgressRepository(other.id, session_maker),
    )
    commit = "a" * 40
    for progress in repositories:
        await progress.target(commit, vector_key="model")
        for stage in ("content", "files", "graph", "vectors"):
            run = await progress.begin(stage, commit)
            assert run is not None and await progress.finish(stage, commit, run)
    active = await repositories[0].begin("graph", commit, reusable_commit=commit)
    vector = await repositories[1].begin("vectors", commit)
    assert active is not None and vector is not None
    async with session_maker() as session, session.begin():
        await invalidate_git_graphs(session)
    assert not await repositories[0].finish("graph", commit, active)
    assert not await repositories[1].finish("vectors", commit, vector)
    for progress in repositories:
        rows = {row.stage: row for row in await progress.read()}
        assert rows["content"].completed_commit == rows["files"].completed_commit == commit
        assert rows["graph"].completed_commit is rows["graph"].reusable_commit is None
        assert rows["vectors"].completed_commit is None
        assert await progress.begin("graph", commit, reusable_commit=commit) is None


@pytest.mark.asyncio
async def test_graph_policy_change_rebuilds_its_vectors_but_not_content(
    test_project, session_maker
):
    progress = GitProgressRepository(test_project.id, session_maker)
    commit = "a" * 40
    await progress.target(commit, vector_key="model", graph_key="graph-1")
    for stage in ("content", "files", "graph", "vectors"):
        run = await progress.begin(stage, commit)
        assert run is not None and await progress.finish(stage, commit, run)
    await progress.target(commit, vector_key="model", graph_key="graph-2")
    rows = {row.stage: row for row in await progress.read()}
    assert rows["content"].completed_commit == rows["files"].completed_commit == commit
    assert rows["graph"].completed_commit is rows["graph"].reusable_commit is None
    assert rows["vectors"].completed_commit is None
