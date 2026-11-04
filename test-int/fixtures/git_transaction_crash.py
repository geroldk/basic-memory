"""Exit the normal API host around private-pointer and publication boundaries."""

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
from basic_memory.repository.git_transaction_repository import GitTransactionRepository
from basic_memory.schemas.git_transactions import GitCommittedState


async def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    phase = request["phase"]
    save, publish = GitTransactionRepository.save, GitProjectRepository.publish

    def save_or_die(self, record):
        if phase == "draft_before" and record.revision == 2:
            os._exit(83)
        save(self, record)
        if (phase == "draft_after" and record.revision == 2) or (
            phase == "receipt_after" and isinstance(record.state, GitCommittedState)
        ):
            os._exit(83)

    def publish_or_die(self, prepared):
        if phase == "git_before":
            os._exit(83)
        result = publish(self, prepared)
        if phase == "git_after":
            os._exit(83)
        return result

    async def paused(self, *, vectors):
        # Target only foreground boundaries; the parent resumes the real worker.
        await asyncio.Event().wait()

    with (
        patch.object(GitTransactionRepository, "save", save_or_die),
        patch.object(GitProjectRepository, "publish", publish_or_die),
        patch.object(GitProjectionWorker, "_loop", paused),
    ):
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                action = "write" if phase.startswith("draft_") else "commit"
                response = await client.post(
                    f"/v2/projects/{request['project_id']}/git/transactions/{request['transaction_id']}/{action}",
                    headers={"X-BM-Transaction-Key": request["key"]},
                    json=request["body"],
                )
                raise RuntimeError(
                    f"Crash boundary not reached: {response.status_code} {response.text}"
                )


if __name__ == "__main__":
    asyncio.run(main())
