"""Published content and historical index progress; no strict snapshot guarantee."""

from typing import Literal

from pydantic import BaseModel

from basic_memory.repository.git_progress_repository import GitProjectionStage


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
