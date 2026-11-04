"""Resolve an explicitly started Git host; never silently use legacy acceptance."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from basic_memory.deps.config import AppConfigDep
from basic_memory.index.git_host import GitHost
from basic_memory.deps.projects import ProjectExternalIdPathDep
from basic_memory.services.git_project_runtime import GitProjectRuntime


async def get_git_host(request: Request, app_config: AppConfigDep) -> GitHost | None:
    if not app_config.git_repositories:
        return None
    # Deferred import: the API composition root otherwise re-enters its routers.
    from basic_memory.api.container import resolve_container

    container = getattr(request.app.state, "container", None)
    if container is None:
        container = resolve_container()
    host = container.git_host
    if host is None:
        raise HTTPException(503, "Git host is not running; start its API or MCP lifecycle")
    return host


GitHostDep = Annotated[GitHost | None, Depends(get_git_host)]


async def get_git_runtime(
    project_id: ProjectExternalIdPathDep, host: GitHostDep
) -> GitProjectRuntime | None:
    if host is None:
        return None
    for runtime in host.runtimes.values():
        if runtime.pipeline.project_id == project_id:
            return runtime
    raise HTTPException(404, "Project is not owned by this Git host")


GitRuntimeDep = Annotated[GitProjectRuntime | None, Depends(get_git_runtime)]
