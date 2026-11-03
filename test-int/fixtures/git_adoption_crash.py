"""Terminate the interpreter around the first-adoption Git publication boundary."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

from basic_memory.db import DatabaseType, engine_session_factory
from basic_memory.models import Project
from basic_memory.repository.git_project_repository import (
    GitProjectRepository,
    GitPublication,
    PreparedGitPublication,
)
from basic_memory.services.git_project_adoption import publish_git_adoption


async def main() -> None:
    database, repository_path, project_id, fault = sys.argv[1:]
    publish = GitProjectRepository.publish

    def crash(self: GitProjectRepository, prepared: PreparedGitPublication) -> GitPublication:
        if fault == "before":
            os._exit(78)
        publish(self, prepared)
        os._exit(78)

    async with engine_session_factory(Path(database), DatabaseType.FILESYSTEM) as factory:
        async with factory[1]() as session:
            project = await session.get(Project, int(project_id))
            assert project is not None
        with patch.object(GitProjectRepository, "publish", crash):
            await publish_git_adoption(
                GitProjectRepository(Path(repository_path)),
                project=project,
                session_maker=factory[1],
            )
    raise RuntimeError("Publication fault boundary was not reached")


if __name__ == "__main__":
    asyncio.run(main())
