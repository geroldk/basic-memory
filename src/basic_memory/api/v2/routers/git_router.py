"""Observe Git publication and eventual local projection without starting work."""

from fastapi import APIRouter, HTTPException

from basic_memory.deps.git import GitHostDep
from basic_memory.schemas.git_status import GitProjectStatus, GitStageStatus

router = APIRouter(prefix="/git", tags=["git"])


@router.get("/status", response_model=GitProjectStatus)
async def get_git_project_status(project_id: str, host: GitHostDep) -> GitProjectStatus:
    if host is None or project_id not in host.runtimes:
        raise HTTPException(404, "Project is not owned by this Git host")
    progress = await host.runtime_for(project_id).worker.status()
    return GitProjectStatus(
        project_id=project_id,
        source_commit=progress.source_commit,
        caught_up=progress.caught_up,
        stages=[
            GitStageStatus(
                stage=row.stage,
                target_commit=row.target_commit,
                completed_commit=row.completed_commit,
                running_commit=row.running_commit,
                attempts=row.attempts,
                last_error=row.last_error,
            )
            for row in progress.stages
        ],
    )
