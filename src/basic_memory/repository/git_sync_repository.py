"""Private atomic sync observations; incoming refs retain the actual histories."""

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basic_memory.models import Entity
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.schemas.git_sync import GitSyncRecord, GitSyncRemote
from basic_memory.schemas.git_project import GitProjectManifest


class GitSyncRepository:
    def __init__(
        self, repository: GitProjectRepository, project_id: UUID, remote: GitSyncRemote | None
    ) -> None:
        self.path = repository.path / "bm-sync.json"
        self.project_id = project_id
        self.fingerprint = hashlib.sha256(remote.url.encode()).hexdigest() if remote else ""

    async def require_note_ownership(
        self, session: AsyncSession, project_id: int, manifest: GitProjectManifest
    ) -> None:
        foreign = await session.scalar(
            select(Entity.id)
            .where(
                Entity.external_id.in_([str(note.external_id) for note in manifest.notes]),
                Entity.project_id != project_id,
            )
            .limit(1)
        )
        if foreign is not None:
            raise ValueError("Incoming note UUID is already owned by another project")

    def load(self) -> GitSyncRecord:
        if self.path.is_symlink():
            raise ValueError("Sync metadata must not use a symlink")
        if self.path.exists():
            record = GitSyncRecord.model_validate_json(self.path.read_bytes())
            if record.project_id != self.project_id:
                raise ValueError("Sync metadata belongs to another project")
            if record.peer_fingerprint == self.fingerprint:
                return record
        return GitSyncRecord(project_id=self.project_id, peer_fingerprint=self.fingerprint)

    def save(self, record: GitSyncRecord) -> None:
        if self.path.is_symlink():
            raise ValueError("Sync metadata must not use a symlink")
        with NamedTemporaryFile(
            mode="wb", dir=self.path.parent, prefix=".sync-", delete=False
        ) as out:
            temporary = Path(out.name)
            try:
                out.write(record.model_dump_json(indent=2).encode())
                out.flush()
                os.fsync(out.fileno())
                os.replace(temporary, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
