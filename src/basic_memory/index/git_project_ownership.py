"""Local project/root binding and OS-released exclusive projection ownership."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, ExitStack
from pathlib import Path
from typing import Literal
from uuid import UUID

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basic_memory.file_utils import write_file_atomic_bytes
from basic_memory.models import Project
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_projection_repository import GitProjectionConflict


class GitLocalBinding(BaseModel):
    """Local-only metadata: never part of the published Git tree or remote sync."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    project_external_id: UUID
    project_root: str
    repository_path: str


async def require_registered_git_root(
    session: AsyncSession, *, project_id: int, project_external_id: str, root: Path
) -> None:
    project = await session.get(Project, project_id)
    if (
        project is None
        or project.external_id != project_external_id
        or Path(project.path).resolve() != root
    ):
        raise GitProjectionConflict("Worker project identity/root differs from SQL registration")


@asynccontextmanager
async def own_git_project(
    repository: GitProjectRepository,
    *,
    project_id: int,
    project_external_id: str,
    project_root: Path,
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Bind the local roots and exclude a second worker, including other processes.

    These are filesystem ownership locks, not Git publication or SQL locks.
    Agent transactions can still plan/publish without holding this ownership.
    Process death releases the OS locks. Never unlink the lock files on release.
    """
    root = project_root.resolve()
    git_root = repository.path.resolve()
    async with session_maker() as session:
        await require_registered_git_root(
            session, project_id=project_id, project_external_id=project_external_id, root=root
        )
        for registered in await session.scalars(select(Project)):
            registered_root = Path(registered.path).resolve()
            if git_root.is_relative_to(registered_root) or registered_root.is_relative_to(git_root):
                raise GitProjectionConflict(
                    "Internal Git repository overlaps an indexed project root"
                )
            if registered.id != project_id and (
                root.is_relative_to(registered_root) or registered_root.is_relative_to(root)
            ):
                raise GitProjectionConflict("Git project root overlaps another registered project")
    manifest = (await asyncio.to_thread(repository.snapshot)).manifest
    if str(manifest.project_external_id) != project_external_id:
        raise GitProjectionConflict("Worker repository belongs to another project UUID")
    control = root / ".basic-memory"
    if control.is_symlink():
        raise GitProjectionConflict("Git project control directory must not be a symlink")
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = GitLocalBinding(
        project_external_id=UUID(project_external_id),
        project_root=str(root),
        repository_path=str(git_root),
    )
    markers = (control / "git-binding.json", git_root / "bm-local-binding.json")
    locks = (control / "git-projection.lock", git_root / "bm-projection.lock")
    with ExitStack() as ownership:
        for lock_path in sorted(locks):
            if lock_path.is_symlink():
                raise GitProjectionConflict("Git worker ownership file must not be a symlink")
            try:
                ownership.enter_context(FileLock(lock_path, timeout=0, thread_local=False))
            except Timeout as error:
                raise GitProjectionConflict(
                    "A Git projection owner is already running for this root/repository"
                ) from error
        # Validate both before writing either. A crash after the first write is
        # recoverable when the retained marker agrees; a different binding is not.
        for marker in markers:
            if marker.is_symlink():
                raise GitProjectionConflict("Git binding metadata must not be a symlink")
            if (
                marker.exists()
                and GitLocalBinding.model_validate_json(marker.read_bytes()) != binding
            ):
                raise GitProjectionConflict(
                    "Git project root/repository is already bound differently"
                )
        for marker in markers:
            if not marker.exists():
                await write_file_atomic_bytes(
                    marker, (binding.model_dump_json(indent=2) + "\n").encode()
                )
        yield
