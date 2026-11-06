"""Per-note three-way merge of two diverged published histories (pure; no Git, no I/O).

The unit of comparison is the note's UUID, never a text line: the manifest is one file that
lists every note, so a line merge would report conflicts between unrelated notes. Each note
is compared as (path, bytes) across base, local and remote; the manifest is rebuilt from the
decisions, never merged.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from basic_memory.file_utils import ParseError, parse_frontmatter
from basic_memory.repository.git_project_repository import GitSnapshot

type ConflictKind = Literal[
    "both_changed", "delete_vs_change", "moved_twice", "path_collision", "permalink_collision"
]


@dataclass(frozen=True, slots=True)
class NoteState:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class MergeConflict:
    kind: ConflictKind
    external_ids: tuple[UUID, ...]
    # Where each side keeps the note(s); "base"/"local"/"remote" -> path, missing side omitted.
    paths: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class MergePlan:
    """The merged tree, or the conflicts that stop it. `notes` is empty when conflicted."""

    notes: Mapping[UUID, NoteState]
    conflicts: tuple[MergeConflict, ...]

    @property
    def files(self) -> dict[str, str]:
        return {note.path: note.content for note in self.notes.values()}

    @property
    def note_ids(self) -> dict[str, UUID]:
        return {note.path: external_id for external_id, note in self.notes.items()}


def note_states(snapshot: GitSnapshot) -> dict[UUID, NoteState]:
    return {
        external_id: NoteState(path, snapshot.files[path])
        for path, external_id in snapshot.manifest.note_ids.items()
    }


def _pick[T](base: T, local: T, remote: T) -> tuple[T, bool]:
    """Three-way choice for one component; the flag says both sides changed it differently."""
    if local == remote or remote == base:
        return local, False
    if local == base:
        return remote, False
    return base, True


def _merge_note(
    external_id: UUID,
    base: NoteState | None,
    local: NoteState | None,
    remote: NoteState | None,
) -> tuple[NoteState | None, MergeConflict | None]:
    # Cases 1-4: one side unchanged, or both sides did the same (also deleting twice).
    if local == remote or base == remote:
        return local, None
    if base == local:
        return remote, None
    sides = tuple(
        (name, state.path)
        for name, state in (("base", base), ("local", local), ("remote", remote))
        if state is not None
    )
    if local is None or remote is None or base is None:
        # Case 7: deleted against changed. A note added twice under one UUID is a bytes clash.
        kind: ConflictKind = "both_changed" if base is None else "delete_vs_change"
        return None, MergeConflict(kind, (external_id,), sides)
    # Case 5 and 6 and 8: path and bytes are separate components.
    path, moved_twice = _pick(base.path, local.path, remote.path)
    content, both_changed = _pick(base.content, local.content, remote.content)
    if moved_twice:
        return None, MergeConflict("moved_twice", (external_id,), sides)
    if both_changed:
        return None, MergeConflict("both_changed", (external_id,), sides)
    return NoteState(path, content), None


def _permalink(content: str) -> str | None:
    try:
        value = parse_frontmatter(content).get("permalink")
    except ParseError:
        return None
    return value if isinstance(value, str) else None


def plan_merge(
    base: GitSnapshot,
    local: GitSnapshot,
    remote: GitSnapshot,
) -> MergePlan:
    """Decide every note, then check the result tree as a whole (cases 9 and 10).

    Projects with a different UUID or manifest format cannot be merged (case 13) and are the
    caller's concern: the incoming-history check already rejects a foreign project.
    """
    states = [note_states(snapshot) for snapshot in (base, local, remote)]
    merged: dict[UUID, NoteState] = {}
    conflicts: list[MergeConflict] = []
    for external_id in sorted({*states[0], *states[1], *states[2]}, key=str):
        decided, conflict = _merge_note(external_id, *(side.get(external_id) for side in states))
        if conflict is not None:
            conflicts.append(conflict)
        elif decided is not None:
            merged[external_id] = decided
    by_path: dict[str, list[UUID]] = defaultdict(list)
    by_permalink: dict[str, list[UUID]] = defaultdict(list)
    for external_id, note in merged.items():
        by_path[note.path].append(external_id)
        if (permalink := _permalink(note.content)) is not None:
            by_permalink[permalink].append(external_id)
    conflicts += [
        MergeConflict("path_collision", tuple(sorted(owners, key=str)))
        for owners in by_path.values()
        if len(owners) > 1
    ]
    conflicts += [
        MergeConflict("permalink_collision", tuple(sorted(owners, key=str)))
        for owners in by_permalink.values()
        if len(owners) > 1
    ]
    if conflicts:
        return MergePlan({}, tuple(conflicts))
    return MergePlan(merged, ())
