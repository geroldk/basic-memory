"""Lifecycle-owned, bounded Git catch-up; Git + SQL survive lost wakeups/restarts."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass

from loguru import logger

from basic_memory.index.git_projection import GitProjectionPipeline
from basic_memory.index.git_project_ownership import own_git_project
from basic_memory.repository.git_progress_repository import (
    GitProgressRepository,
    GitProjectionStage,
    GitStageProgress,
)


@dataclass(frozen=True, slots=True)
class GitProjectProgress:
    source_commit: str
    stages: tuple[GitStageProgress, ...]
    vector_key: str

    @property
    def caught_up(self) -> bool:
        return len(self.stages) == 4 and all(
            stage.target_commit == self.source_commit
            and stage.completed_commit == self.source_commit
            and stage.running_commit is None
            and stage.last_error is None
            and stage.projection_key == (self.vector_key if stage.stage == "vectors" else "1")
            for stage in self.stages
        )


class GitProjectionWorker:
    """Two serial lanes: slow vectors cannot delay content/file/graph catch-up.

    The context owns both tasks. There is no in-memory job backlog: each pass
    samples the latest Git head and durable checkpoints. Partial failed stages
    are retried as a whole. A crash before acknowledgement safely repeats work.
    Local root/repository bindings and OS locks enforce unique ownership.
    """

    def __init__(
        self,
        pipeline: GitProjectionPipeline,
        *,
        poll_interval: float = 5,
        content_gate: asyncio.Lock | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.pipeline = pipeline
        self.progress = GitProgressRepository(pipeline.project_id, pipeline.session_maker)
        self.poll_interval = poll_interval
        self.content_gate = content_gate
        self._wake_index = asyncio.Event()
        self._wake_vectors = asyncio.Event()
        self._running = False
        self.startup_content_commit: str | None = None

    def notify(self) -> None:
        """Optional latency hint after publication; polling never depends on delivery."""
        self._wake_index.set()
        self._wake_vectors.set()

    async def status(self) -> GitProjectProgress:
        await self.pipeline.require_registration()
        return GitProjectProgress(
            await asyncio.to_thread(self.pipeline.repository.head),
            await self.progress.read(),
            self.pipeline.vector_key,
        )

    @asynccontextmanager
    async def run(
        self, *, recover_content_before_yield: bool = False
    ) -> AsyncIterator["GitProjectionWorker"]:
        if self._running:
            raise RuntimeError("Git projection worker is already running")
        self._running = True
        self.startup_content_commit = None
        try:
            async with (
                own_git_project(
                    self.pipeline.repository,
                    project_id=self.pipeline.project_id,
                    project_external_id=self.pipeline.project_external_id,
                    project_root=self.pipeline.file_service.base_path,
                    session_maker=self.pipeline.session_maker,
                ),
                asyncio.TaskGroup() as group,
            ):
                if recover_content_before_yield:
                    # Only SQL content gates admission. Slow files/graph/vectors
                    # start in the background after this explicit startup barrier.
                    commit_id = await asyncio.to_thread(self.pipeline.repository.head)
                    await self.pipeline.recover_content(commit_id, redrive_materialization=False)
                    self.startup_content_commit = commit_id
                index = group.create_task(self._loop(vectors=False), name="git-projection-index")
                vectors = group.create_task(self._loop(vectors=True), name="git-projection-vectors")
                try:
                    yield self
                finally:
                    index.cancel()
                    vectors.cancel()
        finally:
            self._running = False

    async def _loop(self, *, vectors: bool) -> None:
        wake = self._wake_vectors if vectors else self._wake_index
        failures = 0
        while True:
            # Clear before work: a publication during the pass schedules an immediate next pass.
            wake.clear()
            try:
                succeeded = await self._pass(vectors=vectors)
            except Exception:
                # The lifecycle boundary retries storage/DB failures too. Cancellation
                # propagates; a failed checkpoint write can never acknowledge the stage.
                logger.exception("Git projection pass failed", project_id=self.pipeline.project_id)
                succeeded = False
            failures = 0 if succeeded else min(failures + 1, 6)
            delay = self.poll_interval * (2**failures)
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _pass(self, *, vectors: bool) -> bool:
        await self.pipeline.require_registration()
        if not vectors:
            commit_id = await asyncio.to_thread(self.pipeline.repository.head)
            await self.progress.target(commit_id, vector_key=self.pipeline.vector_key)
        rows = {row.stage: row for row in await self.progress.read()}
        stages: tuple[GitProjectionStage, ...] = (
            ("vectors",) if vectors else ("content", "files", "graph")
        )
        for stage in stages:
            row = rows.get(stage)
            if row is None:
                return True
            if stage == "vectors" and rows["graph"].completed_commit != row.target_commit:
                return True
            if (
                row.completed_commit == row.target_commit
                and row.running_commit is None
                and row.last_error is None
            ):
                continue
            run_id = await self.progress.begin(stage, row.target_commit)
            if run_id is None:
                return False
            try:
                gate = self.content_gate if stage == "content" else None
                async with gate if gate is not None else nullcontext():
                    await self.pipeline.apply(stage, row.target_commit)
                    await self.pipeline.require_head(row.target_commit)
            except Exception as error:
                await self.progress.finish(stage, row.target_commit, run_id, error=str(error))
                logger.warning(
                    "Git projection stage pending after failure",
                    project_id=self.pipeline.project_id,
                    stage=stage,
                    commit_id=row.target_commit,
                    error=str(error),
                )
                return False
            if not await self.progress.finish(stage, row.target_commit, run_id):
                return False
            if stage == "graph":
                self._wake_vectors.set()
        return True
