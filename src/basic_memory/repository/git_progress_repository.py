"""Short transactions for independent, retryable Git projection checkpoints."""

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basic_memory.models import GitProjectionProgress

type GitProjectionStage = Literal["content", "files", "graph", "vectors"]
GIT_PROJECTION_STAGES: tuple[GitProjectionStage, ...] = ("content", "files", "graph", "vectors")


@dataclass(frozen=True, slots=True)
class GitStageProgress:
    stage: GitProjectionStage
    projection_key: str
    target_commit: str
    completed_commit: str | None
    running_commit: str | None
    attempts: int
    last_error: str | None


class GitProgressRepository:
    """One runtime owner per project; attempt IDs fence late acknowledgements."""

    def __init__(self, project_id: int, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self.project_id = project_id
        self.session_maker = session_maker

    async def target(self, commit_id: str, *, vector_key: str) -> None:
        async with self.session_maker() as session, session.begin():
            for stage in GIT_PROJECTION_STAGES:
                key = vector_key if stage == "vectors" else "1"
                row = await session.get(GitProjectionProgress, (self.project_id, stage))
                if row is None:
                    session.add(
                        GitProjectionProgress(
                            project_id=self.project_id,
                            stage=stage,
                            projection_key=key,
                            target_commit=commit_id,
                        )
                    )
                elif row.target_commit != commit_id or row.projection_key != key:
                    if row.projection_key != key:
                        row.completed_commit = None
                    row.projection_key = key
                    row.target_commit = commit_id
                    row.running_commit = row.run_id = row.last_error = None
                    row.attempts = 0

    async def read(self) -> tuple[GitStageProgress, ...]:
        async with self.session_maker() as session:
            rows = {
                row.stage: row
                for row in await session.scalars(
                    select(GitProjectionProgress).where(
                        GitProjectionProgress.project_id == self.project_id
                    )
                )
            }
            return tuple(
                GitStageProgress(
                    stage=stage,
                    projection_key=row.projection_key,
                    target_commit=row.target_commit,
                    completed_commit=row.completed_commit,
                    running_commit=row.running_commit,
                    attempts=row.attempts,
                    last_error=row.last_error,
                )
                for stage in GIT_PROJECTION_STAGES
                if (row := rows.get(stage)) is not None
            )

    async def begin(self, stage: GitProjectionStage, commit_id: str) -> str | None:
        run_id = str(uuid4())
        async with self.session_maker() as session, session.begin():
            result = await session.scalar(
                update(GitProjectionProgress)
                .where(
                    GitProjectionProgress.project_id == self.project_id,
                    GitProjectionProgress.stage == stage,
                    GitProjectionProgress.target_commit == commit_id,
                )
                .values(
                    run_id=run_id,
                    running_commit=commit_id,
                    attempts=GitProjectionProgress.attempts + 1,
                    last_error=None,
                )
                .returning(GitProjectionProgress.run_id)
            )
        return result

    async def finish(
        self,
        stage: GitProjectionStage,
        commit_id: str,
        run_id: str,
        *,
        error: str | None = None,
    ) -> bool:
        values: dict[str, str | None] = {
            "running_commit": None,
            "run_id": None,
            "last_error": error,
        }
        if error is None:
            values["completed_commit"] = commit_id
        async with self.session_maker() as session, session.begin():
            result = await session.scalar(
                update(GitProjectionProgress)
                .where(
                    GitProjectionProgress.project_id == self.project_id,
                    GitProjectionProgress.stage == stage,
                    GitProjectionProgress.target_commit == commit_id,
                    GitProjectionProgress.run_id == run_id,
                )
                .values(**values)
                .returning(GitProjectionProgress.project_id)
            )
            return result is not None
