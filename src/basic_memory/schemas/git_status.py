"""Published content and historical index progress; no strict snapshot guarantee."""

from typing import Literal

from pydantic import BaseModel

from basic_memory.schemas.git_sync import GitSyncStatus

type GitProjectionStage = Literal["content", "files", "graph", "vectors"]


class GitStageStatus(BaseModel):
    stage: GitProjectionStage
    target_commit: str
    completed_commit: str | None
    running_commit: str | None
    attempts: int
    last_error: str | None


class GitProjectStatus(BaseModel):
    project_id: str
    source_commit: str
    caught_up: bool
    read_consistency: Literal["relaxed"] = "relaxed"
    stages: list[GitStageStatus]
    synchronization: GitSyncStatus | None = None
