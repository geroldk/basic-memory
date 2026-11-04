"""Kill the whole API interpreter at a real peer/publication/observation boundary."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from basic_memory.api.app import app
from basic_memory.index.git_projection_worker import GitProjectionWorker
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_sync_repository import GitSyncRepository
from basic_memory.schemas.git_sync import SyncCompleted


async def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    phase = request["phase"]
    fetch = GitProjectRepository.fetch_published
    publish = GitProjectRepository.publish_incoming
    push = GitProjectRepository.push_published
    save = GitSyncRepository.save

    def fetch_or_die(self, *args):
        if phase == "fetch_before":
            os._exit(83)
        result = fetch(self, *args)
        if phase == "fetch_after":
            os._exit(83)
        return result

    def publish_or_die(self, *args):
        if phase == "publish_before":
            os._exit(83)
        publish(self, *args)
        if phase == "publish_after":
            os._exit(83)

    def push_or_die(self, *args):
        if phase == "push_before":
            os._exit(83)
        push(self, *args)
        if phase == "push_after":
            os._exit(83)

    def save_or_die(self, record):
        save(self, record)
        if phase == "status_after" and isinstance(record.attempt, SyncCompleted):
            os._exit(83)

    async def paused(self, *, vectors):
        await asyncio.Event().wait()

    with (
        patch.object(GitProjectRepository, "fetch_published", fetch_or_die),
        patch.object(GitProjectRepository, "publish_incoming", publish_or_die),
        patch.object(GitProjectRepository, "push_published", push_or_die),
        patch.object(GitSyncRepository, "save", save_or_die),
        patch.object(GitProjectionWorker, "_loop", paused),
    ):
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                result = await client.post(f"/v2/projects/{request['project_id']}/git/sync")
                raise RuntimeError(
                    f"Crash boundary not reached: {result.status_code} {result.text}"
                )


if __name__ == "__main__":
    asyncio.run(main())
