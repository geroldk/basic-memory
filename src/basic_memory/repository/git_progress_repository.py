"""Short transactions for independent, retryable Git projection checkpoints."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basic_memory.models import GitProjectionProgress
from basic_memory.schemas.git_status import GitProjectionStage as GitProjectionStage

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
    reusable_commit: str | None


async def invalidate_git_graphs(session: AsyncSession) -> None:
    """Structural SQL changes also affect sources linking from other projects.

    Invalidate in/before the mutation transaction, not after publication. Fence
    active runs too: their late acknowledgements cannot restore a reusable basis
    after deletion cascades or identity changes have damaged that graph.
    """
    await session.execute(
        update(GitProjectionProgress)
        .where(GitProjectionProgress.stage.in_(("graph", "vectors")))
        .values(
            reusable_commit=None,
            completed_commit=None,
            running_commit=None,
            run_id=None,
        )
    )


class GitProgressRepository:
    """One runtime owner per project; attempt IDs fence late acknowledgements."""

    def __init__(self, project_id: int, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self.project_id = project_id
        self.session_maker = session_maker

    async def target(self, commit_id: str, *, vector_key: str, graph_key: str = "1") -> None:
        async with self.session_maker() as session, session.begin():
            graph = await session.get(GitProjectionProgress, (self.project_id, "graph"))
            graph_changed = graph is not None and graph.projection_key != graph_key
            for stage in GIT_PROJECTION_STAGES:
                key = vector_key if stage == "vectors" else graph_key if stage == "graph" else "1"
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
                elif (
                    row.target_commit != commit_id
                    or row.projection_key != key
                    or (stage == "vectors" and graph_changed)
                ):
                    if row.projection_key != key or (stage == "vectors" and graph_changed):
                        row.completed_commit = None
                        row.reusable_commit = None
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
                    reusable_commit=row.reusable_commit,
                )
                for stage in GIT_PROJECTION_STAGES
                if (row := rows.get(stage)) is not None
            )

    async def begin(
        self, stage: GitProjectionStage, commit_id: str, *, reusable_commit: str | None = None
    ) -> str | None:
        run_id = str(uuid4())
        statement = update(GitProjectionProgress).where(
            GitProjectionProgress.project_id == self.project_id,
            GitProjectionProgress.stage == stage,
            GitProjectionProgress.target_commit == commit_id,
        )
        if stage == "graph":
            statement = statement.where(
                GitProjectionProgress.reusable_commit == reusable_commit
                if reusable_commit is not None
                else GitProjectionProgress.reusable_commit.is_(None)
            ).values(reusable_commit=None)
        async with self.session_maker() as session, session.begin():
            result = await session.scalar(
                statement.values(
                    run_id=run_id,
                    running_commit=commit_id,
                    attempts=GitProjectionProgress.attempts + 1,
                    last_error=None,
                ).returning(GitProjectionProgress.run_id)
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
            if stage == "graph":
                values["reusable_commit"] = commit_id
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
