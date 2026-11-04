"""Private, complete offline backups; independent from branch synchronization."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from basic_memory.schemas.git_transactions import GitCommitId


class GitBackupFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    size: int = Field(ge=0)
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class GitBackupProject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: UUID
    name: str
    source_root: str
    source_repository: str
    published_commit: GitCommitId
    refs: dict[Annotated[str, Field(pattern=r"^refs/[A-Za-z0-9/._-]+$")], GitCommitId]


class GitBackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    created_at: datetime
    producer_version: str
    source_config: str
    projects: tuple[GitBackupProject, ...]
    files: dict[str, GitBackupFile]
