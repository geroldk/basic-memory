"""An accepted path retirement never grants permission without content evidence."""

from uuid import uuid4

import pytest

from basic_memory.repository.git_layout_repository import GitLayoutRepository, GitLocalNote
from basic_memory.repository.git_projection_repository import GitProjectionConflict


@pytest.mark.asyncio
async def test_missing_content_grants_no_file_cleanup_permission(test_project, session_maker):
    repository = GitLayoutRepository(test_project.id)
    note = GitLocalNote(1, uuid4(), "protected.md", None, "known-file")
    async with session_maker() as session:
        with pytest.raises(GitProjectionConflict, match="without proven accepted content"):
            await repository.retain_old_file(session, note)
        assert not await repository.retirements(session)
