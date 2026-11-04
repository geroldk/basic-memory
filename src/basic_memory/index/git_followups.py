"""Git routes notify the owned worker instead of launching legacy jobs."""

from dataclasses import dataclass

from basic_memory.repository.git_projection_repository import GitProjectionConflict
from basic_memory.runtime.note_content import (
    RuntimeAcceptedNoteChange,
    RuntimeNoteContentResponsePayload,
)
from basic_memory.services.git_project_runtime import GitProjectRuntime


@dataclass(frozen=True, slots=True)
class GitNoteFollowups:
    runtime: GitProjectRuntime

    async def materialize_write_change(
        self, accepted: RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]
    ) -> RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]:
        if (
            accepted.materialization is not None
            or accepted.file_delete is not None
            or accepted.relation_cleanup_entity_ids
        ):
            raise GitProjectionConflict("Git acceptance must not schedule legacy materialization")
        self.runtime.worker.notify()
        return accepted

    async def materialize_delete_change(
        self, accepted: RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]
    ) -> RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]:
        return await self.materialize_write_change(accepted)

    def schedule_relation_resolution(self, *, project_id: int) -> None:
        if project_id != self.runtime.pipeline.project_id:
            raise GitProjectionConflict("Relation follow-up belongs to a different Git project")
        self.runtime.worker.notify()
