"""Exit the whole ASGI process before/after Git and SQL publication boundaries."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch
from typing import NoReturn

from httpx import ASGITransport, AsyncClient

from basic_memory.api.app import app
from basic_memory.config import BasicMemoryConfig
from basic_memory.db import DatabaseType, engine_session_factory
from basic_memory.deps import get_app_config, get_engine_factory
from basic_memory.deps.services import get_note_content_mutation_service
from basic_memory.index.git_projection import build_git_projection_pipeline
from basic_memory.index.git_projection_worker import GitProjectionWorker
from basic_memory.index.local_notes import LocalAcceptedNotePreparerFactory
from basic_memory.indexing.accepted_note_mutation_runner import (
    AcceptedNoteMutationDependencies,
    AcceptedNoteMutationMovePolicy,
)
from basic_memory.models import Project
from basic_memory.repository.accepted_note_repositories import AcceptedNoteRepositories
from basic_memory.repository.git_project_repository import (
    GitProjectRepository,
    GitPublication,
    PreparedGitPublication,
)
from basic_memory.repository.project_repository import ProjectRepository
from basic_memory.services.git_project_runtime import GitProjectRuntime
from basic_memory.services.note_content_writes import NoteContentMutationService


async def main() -> NoReturn:
    database, repository_path, project_id, config_path, fault = sys.argv[1:]
    config = BasicMemoryConfig.model_validate_json(Path(config_path).read_bytes())
    published = False
    publish = GitProjectRepository.publish

    def publish_or_crash(
        self: GitProjectRepository, prepared: PreparedGitPublication
    ) -> GitPublication:
        nonlocal published
        if fault == "git_before":
            os._exit(79)
        result = publish(self, prepared)
        published = True
        if fault == "git_after":
            os._exit(79)
        return result

    async def paused_worker(self: GitProjectionWorker, *, vectors: bool) -> None:
        # Keep the tested SQL commit boundary in the foreground ASGI mutation.
        # The parent restarts with the real unpaused catch-up worker afterward.
        await asyncio.Event().wait()

    async with engine_session_factory(Path(database), DatabaseType.FILESYSTEM) as factory:
        async with factory[1]() as session:
            project = await session.get(Project, int(project_id))
            assert project is not None
        pipeline = build_git_projection_pipeline(
            GitProjectRepository(Path(repository_path)),
            project=project,
            session_maker=factory[1],
            app_config=config,
        )
        repositories = AcceptedNoteRepositories()
        dependencies = AcceptedNoteMutationDependencies(
            project_repository=ProjectRepository(),
            lookup_repositories=repositories,
            write_repositories=repositories,
            preparer_factory=LocalAcceptedNotePreparerFactory(
                session_maker=factory[1], app_config=config
            ),
            move_policy=AcceptedNoteMutationMovePolicy(
                update_permalinks_on_move=config.update_permalinks_on_move
            ),
            verify_storage_absent_on_create=True,
        )
        runtime = GitProjectRuntime(pipeline, dependencies)

        async def mutation_service() -> NoteContentMutationService:
            return NoteContentMutationService(
                session_maker=factory[1],
                mutation_dependencies=dependencies,
                mutation_executor=runtime,
            )

        async def engine_factory():
            return factory

        app.dependency_overrides[get_note_content_mutation_service] = mutation_service
        app.dependency_overrides[get_engine_factory] = engine_factory
        app.dependency_overrides[get_app_config] = lambda: config
        dialect = factory[0].sync_engine.dialect
        do_commit = dialect.do_commit

        def commit_or_crash(connection):
            if published and fault == "sql_before":
                os._exit(79)
            do_commit(connection)
            if published and fault == "sql_after":
                os._exit(79)

        with (
            patch.object(GitProjectRepository, "publish", publish_or_crash),
            patch.object(GitProjectionWorker, "_loop", paused_worker),
            patch.object(dialect, "do_commit", commit_or_crash),
        ):
            async with (
                runtime.run(),
                AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
            ):
                response = await client.post(
                    f"/v2/projects/{project.external_id}/knowledge/write",
                    json={
                        "note": {
                            "title": "Crash",
                            "directory": "notes",
                            "content": "- [fact] Crashprobe\n",
                        }
                    },
                )
                raise RuntimeError(
                    f"ASGI crash boundary not reached: {response.status_code} {response.text}"
                )


if __name__ == "__main__":
    asyncio.run(main())
