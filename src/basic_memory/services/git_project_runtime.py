"""Opt-in Git acceptance for a lifecycle-owned, externally controlled local project.

Configured API/MCP hosts select this executor and its explicit transaction service.
The composition root suppresses legacy writers/materializers/imports before admission.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import assert_never
from uuid import UUID, uuid4

from basic_memory import db
from basic_memory.file_utils import compute_checksum
from basic_memory.index.git_projection import GitProjectionPipeline
from basic_memory.index.git_projection_worker import GitProjectionWorker
from basic_memory.indexing.accepted_note_mutation_runner import (
    AcceptedNoteCreateMutation,
    AcceptedNoteDeleteMutation,
    AcceptedNoteEditMutation,
    AcceptedNoteMoveMutation,
    AcceptedNoteMutationDependencies,
    AcceptedNoteUpdateMutation,
    run_accepted_note_create,
    run_accepted_note_delete,
    run_accepted_note_edit,
    run_accepted_note_move,
    run_accepted_note_update,
)
from basic_memory.models import NoteContent
from basic_memory.repository.git_layout_repository import GitLayoutRepository
from basic_memory.repository.git_graph_inputs import git_graph_identities_unchanged
from basic_memory.repository.git_progress_repository import invalidate_git_graphs
from basic_memory.repository.git_project_repository import (
    GitPublication,
    GitPublicationConflict,
    settle_git_publication,
)
from basic_memory.repository.git_projection_repository import GitProjectionConflict
from basic_memory.runtime.project_partition import RuntimeProjectNoteOperation
from basic_memory.services.note_content_writes import (
    AcceptedNoteChange,
    AcceptedNoteMutationRequest,
    NoteContentPublicationConflict,
    NoteContentMutationServiceError,
)
from basic_memory.services.git_transactions import GitTransactionService
from basic_memory.schemas.git_sync import GitSyncRemote
from basic_memory.services.git_sync import GitSyncService


class GitProjectRuntime:
    """Capture Git basis before real BM preparation; publish before SQL commit.

    Only short implicit mutation/preparation work holds the local admission gate.
    Agent draft thinking must never run inside accept(). Git CAS still arbitrates
    external publications. The worker and this executor share only the SQL content
    gate; vector work never holds it.
    """

    def __init__(
        self,
        pipeline: GitProjectionPipeline,
        dependencies: AcceptedNoteMutationDependencies,
        *,
        poll_interval: float = 5,
        transaction_idle_seconds: int = 1800,
        transaction_retention_seconds: int = 604800,
        sync_remote: GitSyncRemote | None = None,
        sync_merge: bool = False,
    ) -> None:
        self.pipeline = pipeline
        self.dependencies = dependencies
        self.gate = asyncio.Lock()
        self.worker = GitProjectionWorker(
            pipeline, poll_interval=poll_interval, content_gate=self.gate
        )
        self._running = False
        self._sql_commit: str | None = None
        self.last_publication: GitPublication | None = None
        self.transactions = GitTransactionService(
            self,
            idle_seconds=transaction_idle_seconds,
            retention_seconds=transaction_retention_seconds,
        )
        self.sync = GitSyncService(self, sync_remote, merge=sync_merge)

    @asynccontextmanager
    async def run(self) -> AsyncIterator["GitProjectRuntime"]:
        async with self.worker.run(recover_content_before_yield=True):
            self._sql_commit = self.worker.startup_content_commit
            self._running = True
            try:
                await self.transactions.maintain()
                await self.sync.recover()
                yield self
            finally:
                self._running = False
                # Bounded transfers may outlive their caller. Drain them before
                # another process can acquire this project's repository ownership.
                async with self.sync.lock:
                    pass
                # An ASGI host must drain request tasks before exiting its lifespan.
                # Also wait for any admitted mutation before releasing ownership.
                async with self.gate:
                    pass

    @asynccontextmanager
    async def admission(self) -> AsyncIterator[None]:
        """Short call ownership; explicit drafts release it between agent calls."""
        if not self._running:
            raise GitProjectionConflict("Git project is not accepting mutations")
        async with self.gate:
            if not self._running:
                raise GitProjectionConflict("Git project stopped before mutation admission")
            await self.pipeline.require_registration()
            yield

    async def accept(self, request: AcceptedNoteMutationRequest) -> AcceptedNoteChange:
        if request.project_external_id != self.pipeline.project_external_id:
            raise GitProjectionConflict("Mutation belongs to a different Git project")
        async with self.admission():
            repository = self.pipeline.repository
            # This immutable basis is captured before any runner/preparer reads SQL.
            base = await asyncio.to_thread(repository.snapshot)
            if self._sql_commit != base.commit_id:
                await self.pipeline.apply("content", base.commit_id)
                self._sql_commit = base.commit_id
            layout = GitLayoutRepository(self.pipeline.project_id)
            publication: GitPublication | None = None
            try:
                async with db.scoped_session(self.pipeline.session_maker) as session:
                    before = await layout.local_notes(session)
                    if {
                        note.file_path: note.external_id for note in before
                    } != base.manifest.note_ids:
                        raise GitProjectionConflict("SQL content is outside the captured Git basis")
                    for note in before:
                        if (
                            note.content is None
                            or note.content.markdown_content != base.files[note.file_path]
                            or note.content.checksum
                            != await compute_checksum(note.content.markdown_content)
                        ):
                            raise GitProjectionConflict(
                                "SQL content is outside the captured Git basis"
                            )
                    match request:
                        case AcceptedNoteCreateMutation():
                            result = await run_accepted_note_create(
                                session, request=request, dependencies=self.dependencies
                            )
                        case AcceptedNoteUpdateMutation():
                            result = await run_accepted_note_update(
                                session, request=request, dependencies=self.dependencies
                            )
                        case AcceptedNoteEditMutation():
                            result = await run_accepted_note_edit(
                                session, request=request, dependencies=self.dependencies
                            )
                        case AcceptedNoteMoveMutation():
                            result = await run_accepted_note_move(
                                session, request=request, dependencies=self.dependencies
                            )
                        case AcceptedNoteDeleteMutation():
                            result = await run_accepted_note_delete(
                                session, request=request, dependencies=self.dependencies
                            )
                        case _:  # pragma: no cover - exhaustiveness is checked by ty
                            assert_never(request)
                    change = result.change.project_change
                    if change is None:
                        # Already absent DELETE has no effective content mutation.
                        return result.change
                    files, note_ids = dict(base.files), base.manifest.note_ids
                    note_id = UUID(change.note_external_id)
                    if change.operation in {
                        RuntimeProjectNoteOperation.moved,
                        RuntimeProjectNoteOperation.deleted,
                    }:
                        source_path = change.previous_file_path or change.file_path
                        source = next(
                            (note for note in before if note.external_id == note_id), None
                        )
                        if source is None or note_ids.get(source_path) != note_id:
                            raise GitProjectionConflict("Mutation source is outside its Git basis")
                        await layout.retain_old_file(session, source)
                        del files[source_path]
                        del note_ids[source_path]
                    if change.operation is not RuntimeProjectNoteOperation.deleted:
                        content = await session.get(NoteContent, change.entity_id)
                        if (
                            content is None
                            or content.external_id != str(note_id)
                            or content.db_checksum
                            != await compute_checksum(content.markdown_content)
                        ):
                            raise GitProjectionConflict(
                                "Accepted mutation has inconsistent content"
                            )
                        files[change.file_path] = content.markdown_content
                        note_ids[change.file_path] = note_id
                    if not git_graph_identities_unchanged(
                        base.files, base.manifest.note_ids, files, note_ids
                    ):
                        await invalidate_git_graphs(session)
                    prepared = await asyncio.to_thread(
                        repository.prepare,
                        transaction_id=uuid4(),
                        base_commit=base.commit_id,
                        files=files,
                        note_ids=note_ids,
                    )
                    publication = await settle_git_publication(repository, prepared)
                    self.last_publication = publication
                self._sql_commit = publication.commit_id
                # Legacy route materialization must not run after this executor.
                # The lifecycle worker alone owns files/graph/FTS/vectors.
                return replace(
                    result.change,
                    materialization=None,
                    file_delete=None,
                    relation_cleanup_entity_ids=frozenset(),
                )
            except GitPublicationConflict as error:
                raise NoteContentPublicationConflict(
                    {
                        "message": "Git project advanced during preparation",
                        "base_commit": error.expected,
                        "source_commit": error.actual,
                    },
                ) from error
            except Exception as error:
                if publication is not None:
                    raise NoteContentMutationServiceError(
                        503,
                        {
                            "message": "Git published; SQL pending. Check status before retry.",
                            "published_commit": publication.commit_id,
                        },
                    ) from error
                raise
            finally:
                # Even a cancelled/lost publication response or failed SQL commit
                # must wake durable Git recovery. Polling covers process death.
                self.worker.notify()
