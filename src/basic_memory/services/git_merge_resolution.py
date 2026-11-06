"""Turn a person's decisions in a review package into final note states for the merge.

The package (`konflikte.json`) lists each conflict with an empty ``decision``. A person, or a
session at their word, fills it in; nothing here is reachable through the remote gateway.

Single-note conflicts take ``{"action": ...}``:
  both_changed      lokal | fern | beide | bearbeitet
  delete_vs_change  lokal | fern | loeschen | behalten | bearbeitet
  moved_twice       lokal | fern | bearbeitet (with "path")
``bearbeitet`` reads the finished text from ``bearbeitet.md`` in the note's directory.
``beide`` keeps the local note and adds the remote version as a new, marked note.
Collisions take ``{"notes": {"<uuid>": {"path": "new/place.md", "bearbeitet": true}}}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import frontmatter
from pydantic import BaseModel, ConfigDict

from basic_memory.file_utils import dump_frontmatter
from basic_memory.repository.git_project_repository import GitSnapshot
from basic_memory.schemas.git_project import validate_note_path
from basic_memory.services.git_merge import NoteState, merge_note, note_states, pick

type Action = Literal["lokal", "fern", "beide", "bearbeitet", "loeschen", "behalten"]

ALLOWED: dict[str, frozenset[str]] = {
    "both_changed": frozenset({"lokal", "fern", "beide", "bearbeitet"}),
    "delete_vs_change": frozenset({"lokal", "fern", "loeschen", "behalten", "bearbeitet"}),
    "moved_twice": frozenset({"lokal", "fern", "bearbeitet"}),
}
COLLISIONS = frozenset({"path_collision", "permalink_collision"})
EDITED_FILE = "bearbeitet.md"


class ReviewIncomplete(ValueError):
    """The package cannot be applied: undecided or invalid decisions."""


class NoteEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str | None = None
    bearbeitet: bool = False


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Action | None = None
    path: str | None = None
    notes: dict[UUID, NoteEdit] | None = None


@dataclass(frozen=True, slots=True)
class Resolved:
    decided: dict[UUID, NoteState | None]
    reviewed: frozenset[UUID]
    review_id: UUID


def conflict_key(entry: dict[str, Any]) -> str:
    """Identity of a conflict across recomputations: its kind and the exact versions involved."""
    return json.dumps(
        [entry["kind"], [[n["external_id"], n["sha256"], n["paths"]] for n in entry["notes"]]],
        sort_keys=True,
    )


def load_document(directory: Path) -> dict[str, Any]:
    try:
        document = json.loads((directory / "konflikte.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReviewIncomplete(
            f"Review package {directory.name} is missing or unreadable"
        ) from error
    if document.get("format_version") != 1:
        raise ReviewIncomplete("Unsupported review package format")
    return document


def require_decided(directory: Path) -> dict[str, Any]:
    document = load_document(directory)
    open_ones = [c["number"] for c in document["conflicts"] if not c.get("decision")]
    if open_ones:
        raise ReviewIncomplete(f"Undecided conflicts: {open_ones}")
    return document


def _edited_text(directory: Path, number: int, external_id: UUID) -> str:
    file = directory / f"{number:03d}" / str(external_id) / EDITED_FILE
    try:
        return file.read_text(encoding="utf-8")
    except OSError as error:
        raise ReviewIncomplete(
            f"Conflict {number}: {EDITED_FILE} is missing for {external_id}"
        ) from error


def _pick_content(base: NoteState | None, local: NoteState, remote: NoteState) -> str:
    content, both_changed = pick(base.content if base else None, local.content, remote.content)
    if both_changed:
        # moved twice and changed on both sides: bytes need their own decision
        raise ReviewIncomplete("Bytes also differ on both sides: use action 'bearbeitet'")
    assert content is not None
    return content


def _fern_copy(remote: NoteState) -> NoteState:
    """The remote version as an independent, visibly marked note."""
    post = frontmatter.loads(remote.content)
    post.metadata["title"] = f"{post.metadata.get('title', 'Notiz')} (fern)"
    post.metadata["permalink"] = f"{post.metadata['permalink']}-fern"
    path = PurePosixPath(remote.path)
    return NoteState(str(path.with_name(f"{path.stem}-fern{path.suffix}")), dump_frontmatter(post))


def _single(
    directory: Path,
    number: int,
    kind: str,
    external_id: UUID,
    decision: Decision,
    sides: tuple[NoteState | None, NoteState | None, NoteState | None],
    package: str,
) -> tuple[dict[UUID, NoteState | None], set[UUID]]:
    base, local, remote = sides
    action = decision.action
    if action not in ALLOWED[kind]:
        raise ReviewIncomplete(f"Conflict {number}: action {action!r} not allowed for {kind}")
    out: dict[UUID, NoteState | None] = {}
    reviewed: set[UUID] = set()
    match action:
        case "lokal":
            out[external_id] = (
                NoteState(local.path, _pick_content(base, local, remote))
                if kind == "moved_twice" and local and remote
                else local
            )
        case "fern":
            out[external_id] = (
                NoteState(remote.path, _pick_content(base, local, remote))
                if kind == "moved_twice" and local and remote
                else remote
            )
        case "loeschen":
            out[external_id] = None
        case "behalten":
            out[external_id] = local or remote
        case "beide":
            assert local is not None and remote is not None
            twin = uuid5(NAMESPACE_URL, f"{package}/{external_id}")
            out[external_id], out[twin] = local, _fern_copy(remote)
            reviewed.add(twin)
        case "bearbeitet":
            survivor = local or remote or base
            assert survivor is not None
            path = decision.path or (survivor.path if kind != "moved_twice" else None)
            if path is None:
                raise ReviewIncomplete(f"Conflict {number}: 'path' is required after a double move")
            out[external_id] = NoteState(path, _edited_text(directory, number, external_id))
            reviewed.add(external_id)
    for state in out.values():
        if state is not None:
            try:
                validate_note_path(state.path)
            except ValueError as error:
                raise ReviewIncomplete(f"Conflict {number}: {error}") from error
    return out, reviewed


def resolve_package(
    directory: Path, base: GitSnapshot, local: GitSnapshot, remote: GitSnapshot
) -> Resolved:
    """Final states for every decided note; the caller re-plans and re-checks the whole tree."""
    document = require_decided(directory)
    package = directory.name
    sides = [note_states(snapshot) for snapshot in (base, local, remote)]
    decided: dict[UUID, NoteState | None] = {}
    reviewed: set[UUID] = set()
    for entry in document["conflicts"]:
        number, kind = entry["number"], entry["kind"]
        decision = Decision.model_validate(entry["decision"])
        ids = [UUID(n["external_id"]) for n in entry["notes"]]
        if kind in COLLISIONS:
            if not decision.notes or not set(decision.notes) <= set(ids):
                raise ReviewIncomplete(f"Conflict {number}: name the notes to change")
            for external_id in ids:
                planned, conflict = merge_note(external_id, *(s.get(external_id) for s in sides))
                if conflict is not None or planned is None:
                    raise ReviewIncomplete(
                        f"Conflict {number}: note {external_id} is itself in conflict"
                    )
                edit = decision.notes.get(external_id)
                if edit is not None:
                    path = edit.path or planned.path
                    text = (
                        _edited_text(directory, number, external_id)
                        if edit.bearbeitet
                        else planned.content
                    )
                    planned = NoteState(path, text)
                    reviewed.add(external_id)
                    try:
                        validate_note_path(path)
                    except ValueError as error:
                        raise ReviewIncomplete(f"Conflict {number}: {error}") from error
                decided[external_id] = planned
            continue
        (external_id,) = ids
        one_base, one_local, one_remote = (s.get(external_id) for s in sides)
        states, marked = _single(
            directory,
            number,
            kind,
            external_id,
            decision,
            (one_base, one_local, one_remote),
            package,
        )
        decided.update(states)
        reviewed |= marked
    return Resolved(decided, frozenset(reviewed), uuid5(NAMESPACE_URL, package))
