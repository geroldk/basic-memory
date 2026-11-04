"""Typed API transport for private local Git transactions."""

from uuid import UUID
from typing import Literal

from httpx import AsyncClient

from basic_memory.schemas.git_transactions import (
    GitTransactionCommit,
    GitTransactionControl,
    GitTransactionDelete,
    GitTransactionListing,
    GitTransactionMove,
    GitTransactionRead,
    GitTransactionStart,
    GitTransactionStarted,
    GitTransactionStatus,
    GitTransactionWrite,
)


class GitTransactionClient:
    def __init__(self, client: AsyncClient, project_id: str) -> None:
        self.client = client
        self.path = f"/v2/projects/{project_id}/git/transactions"

    async def start(self, request: GitTransactionStart) -> GitTransactionStarted:
        from basic_memory.mcp.tools.utils import call_post

        response = await call_post(self.client, self.path, json=request.model_dump(mode="json"))
        return GitTransactionStarted.model_validate(response.json())

    async def status(
        self, identity: UUID, key: str, *, keep_alive: bool = False
    ) -> GitTransactionStatus:
        from basic_memory.mcp.tools.utils import call_get, call_post

        path = f"{self.path}/{identity}"
        headers = {"X-BM-Transaction-Key": key}
        response = (
            await call_post(self.client, path + "/keep-alive", headers=headers)
            if keep_alive
            else await call_get(self.client, path, headers=headers)
        )
        return GitTransactionStatus.model_validate(response.json())

    async def listing(
        self, identity: UUID, key: str, *, view: Literal["base", "draft"] = "draft"
    ) -> GitTransactionListing:
        from basic_memory.mcp.tools.utils import call_get

        response = await call_get(
            self.client,
            f"{self.path}/{identity}/notes",
            headers={"X-BM-Transaction-Key": key},
            params={"view": view},
        )
        return GitTransactionListing.model_validate(response.json())

    async def read(
        self, identity: UUID, key: str, identifier: str, *, view: Literal["base", "draft"] = "draft"
    ) -> GitTransactionRead:
        from basic_memory.mcp.tools.utils import call_get

        response = await call_get(
            self.client,
            f"{self.path}/{identity}/note",
            headers={"X-BM-Transaction-Key": key},
            params={"identifier": identifier, "view": view},
        )
        return GitTransactionRead.model_validate(response.json())

    async def change(
        self,
        identity: UUID,
        key: str,
        request: GitTransactionWrite
        | GitTransactionMove
        | GitTransactionDelete
        | GitTransactionCommit
        | GitTransactionControl,
    ) -> GitTransactionStatus:
        from basic_memory.mcp.tools.utils import call_post

        match request:
            case GitTransactionWrite():
                action = "write"
            case GitTransactionMove():
                action = "move"
            case GitTransactionDelete():
                action = "delete"
            case GitTransactionCommit():
                action = "commit"
            case GitTransactionControl():
                action = "rollback"
        response = await call_post(
            self.client,
            f"{self.path}/{identity}/{action}",
            headers={"X-BM-Transaction-Key": key},
            json=request.model_dump(mode="json"),
        )
        return GitTransactionStatus.model_validate(response.json())
