"""Route-facing post-acceptance capability; runtime owns the actual work."""

from typing import Protocol

from basic_memory.runtime.note_content import (
    RuntimeAcceptedNoteChange,
    RuntimeNoteContentResponsePayload,
)


class NoteContentMaterializationProvider(Protocol):
    async def materialize_write_change(
        self, accepted: RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]
    ) -> RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]: ...

    async def materialize_delete_change(
        self, accepted: RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]
    ) -> RuntimeAcceptedNoteChange[RuntimeNoteContentResponsePayload]: ...
