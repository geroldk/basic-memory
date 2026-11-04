"""Explicit opportunistic sync. Network waits never hold local mutation admission."""

import asyncio
import subprocess
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from basic_memory.repository.git_project_repository import (
    GitPublicationConflict,
    GitPublicationError,
    GitSnapshot,
    settle_git_operation,
)
from basic_memory.repository.git_sync_repository import GitSyncRepository
from basic_memory.repository.git_transaction_repository import GitTransactionError
from basic_memory.schemas.git_sync import (
    GitSyncRecord,
    GitSyncRemote,
    GitSyncStatus,
    SyncCompleted,
    SyncFailed,
    SyncRunning,
)
from basic_memory.services.git_transactions import validate_git_draft

if TYPE_CHECKING:  # pragma: no cover
    from basic_memory.services.git_project_runtime import GitProjectRuntime


class GitSyncBusy(RuntimeError):
    """A second call must not compete with this project's current sync attempt."""


class GitSyncService:
    def __init__(self, runtime: "GitProjectRuntime", remote: GitSyncRemote | None) -> None:
        self.runtime = runtime
        self.remote = remote
        self.lock = asyncio.Lock()
        self.records = GitSyncRepository(
            runtime.pipeline.repository, UUID(runtime.pipeline.project_external_id), remote
        )

    async def recover(self) -> None:
        record = await asyncio.to_thread(self.records.load)
        if isinstance(record.attempt, SyncRunning):
            await self.save(
                record.model_copy(
                    update={
                        "attempt": SyncFailed(
                            attempt_id=record.attempt.attempt_id,
                            started_at=record.attempt.started_at,
                            finished_at=datetime.now(UTC),
                            reason="interrupted",
                        )
                    }
                )
            )

    async def save(self, record: GitSyncRecord) -> None:
        await settle_git_operation(partial(self.records.save, record))

    async def status(self) -> GitSyncStatus:
        record = await asyncio.to_thread(self.records.load)
        repository = self.runtime.pipeline.repository
        local = await asyncio.to_thread(repository.head)
        relationship = await asyncio.to_thread(
            repository.relationship, local, record.observed_remote_commit
        )
        return GitSyncStatus(
            configured=self.remote is not None,
            local_commit=local,
            observed_remote_commit=record.observed_remote_commit,
            relationship=relationship,
            last_attempt=record.attempt,
        )

    async def synchronize(self) -> GitSyncStatus:
        if self.remote is None:
            raise ValueError("No Git peer configured for this project")
        if self.lock.locked():
            raise GitSyncBusy("A sync attempt is already running for this project")
        async with self.lock:
            # Only the lifecycle owner may start a transfer; never hold this
            # gate across network I/O or full incoming-history validation.
            async with self.runtime.admission():
                record = await asyncio.to_thread(self.records.load)
                attempt = SyncRunning(attempt_id=uuid4(), started_at=datetime.now(UTC))
                record = record.model_copy(update={"attempt": attempt})
                await self.save(record)
            repository = self.runtime.pipeline.repository
            failure_reason = "transport"
            try:
                incoming = await settle_git_operation(
                    partial(repository.fetch_published, self.remote.url, attempt.attempt_id)
                )
                previous = record.observed_remote_commit
                rewritten = previous is not None and (
                    incoming is None
                    or not await asyncio.to_thread(repository.is_ancestor, previous, incoming)
                )
                if rewritten:
                    outcome = "remote_rewritten"
                else:
                    validated_tip: GitSnapshot | None = None
                    if incoming is not None:
                        failure_reason = "invalid_incoming"
                        snapshots = await asyncio.to_thread(
                            repository.incoming_snapshots, attempt.attempt_id, incoming
                        )
                        validated_tip = snapshots[0]
                        for snapshot in snapshots:
                            if snapshot.manifest.project_external_id != record.project_id:
                                raise ValueError("Incoming history belongs to another project")
                            await validate_git_draft(
                                snapshot.files, self.runtime.pipeline.file_service.base_path
                            )
                    failure_reason = "local_changed"
                    async with self.runtime.admission():
                        local = await asyncio.to_thread(repository.head)
                        relationship = await asyncio.to_thread(
                            repository.relationship, local, incoming
                        )
                        if relationship == "behind":
                            assert incoming is not None and validated_tip is not None
                            failure_reason = "invalid_incoming"
                            async with self.runtime.pipeline.session_maker() as session:
                                await self.records.require_note_ownership(
                                    session,
                                    self.runtime.pipeline.project_id,
                                    validated_tip.manifest,
                                )
                            failure_reason = "local_changed"
                            await settle_git_operation(
                                partial(
                                    repository.publish_incoming, attempt.attempt_id, incoming, local
                                )
                            )
                    if relationship in {"ahead", "unobserved"}:
                        failure_reason = "transport"
                        await settle_git_operation(
                            partial(repository.push_published, self.remote.url, local)
                        )
                        incoming = local
                        outcome = "pushed"
                    elif relationship == "behind":
                        outcome = "pulled"
                    else:
                        outcome = relationship
                record = record.model_copy(
                    update={
                        "observed_remote_commit": previous if rewritten else incoming,
                        "attempt": SyncCompleted(
                            attempt_id=attempt.attempt_id,
                            started_at=attempt.started_at,
                            finished_at=datetime.now(UTC),
                            outcome=outcome,
                        ),
                    }
                )
            except asyncio.CancelledError:
                record = record.model_copy(
                    update={
                        "attempt": SyncFailed(
                            attempt_id=attempt.attempt_id,
                            started_at=attempt.started_at,
                            finished_at=datetime.now(UTC),
                            reason="interrupted",
                        )
                    }
                )
                await self.save(record)
                raise
            except (
                GitPublicationError,
                subprocess.TimeoutExpired,
                OSError,
                ValueError,
                GitTransactionError,
            ) as error:
                if isinstance(error, GitPublicationConflict):
                    failure_reason = "local_changed"
                record = record.model_copy(
                    update={
                        "attempt": SyncFailed(
                            attempt_id=attempt.attempt_id,
                            started_at=attempt.started_at,
                            finished_at=datetime.now(UTC),
                            reason=failure_reason,
                        )
                    }
                )
            finally:
                # Git may have changed before a lost response or process failure.
                # The ordinary worker re-reads canonical main, never incoming refs.
                self.runtime.worker.notify()
            await self.save(record)
            return await self.status()
