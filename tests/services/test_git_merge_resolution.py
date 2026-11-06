"""Decisions in a review package become final note states; every conflict kind and refusal."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from basic_memory.repository.git_project_repository import GitSnapshot
from basic_memory.schemas.git_project import GitNoteIdentity, GitProjectManifest
from basic_memory.services.git_merge import NoteState, plan_merge
from basic_memory.services.git_merge_resolution import ReviewIncomplete, resolve_package
from basic_memory.services.git_merge_review import write_review_package

PROJECT = uuid4()
A, B = UUID(int=1), UUID(int=2)


def doc(permalink: str, body: str = "x") -> str:
    return f"---\ntitle: {permalink}\ntype: note\npermalink: {permalink}\n---\n{body}\n"


def snap(commit: str, notes: dict[UUID, tuple[str, str]]) -> GitSnapshot:
    manifest = GitProjectManifest(
        project_external_id=PROJECT,
        notes=tuple(GitNoteIdentity(path=p, external_id=i) for i, (p, _) in notes.items()),
    )
    return GitSnapshot(commit * 40, {p: c for p, c in notes.values()}, manifest)


def package(tmp_path: Path, base, local, remote, decisions, edited: dict[str, str] | None = None):
    b, lo, r = snap("b", base), snap("c", local), snap("d", remote)
    plan = plan_merge(b, lo, r)
    directory = write_review_package(
        tmp_path, project_id=PROJECT, base=b, local=lo, remote=r, plan=plan
    )
    document = json.loads((directory / "konflikte.json").read_text())
    for conflict, decision in zip(document["conflicts"], decisions, strict=True):
        conflict["decision"] = decision
    (directory / "konflikte.json").write_text(json.dumps(document))
    for relative, text in (edited or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return directory, b, lo, r


def resolve(tmp_path, base, local, remote, decisions, edited=None):
    directory, b, lo, r = package(tmp_path, base, local, remote, decisions, edited)
    resolved = resolve_package(directory, b, lo, r)
    return resolved, plan_merge(b, lo, r, decided=resolved.decided), directory


X = ("a.md", doc("a"))
MOVED = ("one/a.md", doc("a"))
MOVED2 = ("two/a.md", doc("a"))
EDIT = ("a.md", doc("a", "mine"))
EDIT2 = ("a.md", doc("a", "theirs"))


def test_delete_against_change_has_four_answers(tmp_path: Path) -> None:
    for action, expected in (
        ("lokal", None),
        ("fern", NoteState(*EDIT2)),
        ("loeschen", None),
        ("behalten", NoteState(*EDIT2)),
    ):
        resolved, plan, _ = resolve(tmp_path / action, {A: X}, {}, {A: EDIT2}, [{"action": action}])
        assert resolved.decided[A] == expected
        assert (A in plan.notes) == (expected is not None) and not plan.conflicts


def test_delete_against_change_can_be_replaced_by_an_edited_text(tmp_path: Path) -> None:
    resolved, plan, _ = resolve(
        tmp_path,
        {A: X},
        {},
        {A: EDIT2},
        [{"action": "bearbeitet"}],
        {f"001/{A}/bearbeitet.md": doc("a", "final")},
    )
    assert plan.notes[A] == NoteState("a.md", doc("a", "final")) and A in resolved.reviewed


def test_double_move_takes_the_chosen_path_with_the_merged_bytes(tmp_path: Path) -> None:
    for action, path in (("lokal", "one/a.md"), ("fern", "two/a.md")):
        _, plan, _ = resolve(
            tmp_path / action, {A: X}, {A: MOVED}, {A: MOVED2}, [{"action": action}]
        )
        assert plan.note_ids == {path: A}


def test_double_move_with_diverging_bytes_needs_an_edited_text_and_path(tmp_path: Path) -> None:
    local, remote = {A: ("one/a.md", doc("a", "mine"))}, {A: ("two/a.md", doc("a", "theirs"))}
    with pytest.raises(ReviewIncomplete, match="Bytes also differ"):
        resolve(tmp_path / "x", {A: X}, local, remote, [{"action": "lokal"}])
    with pytest.raises(ReviewIncomplete, match="'path' is required"):
        resolve(
            tmp_path / "y",
            {A: X},
            local,
            remote,
            [{"action": "bearbeitet"}],
            {f"001/{A}/bearbeitet.md": doc("a")},
        )
    _, plan, _ = resolve(
        tmp_path / "z",
        {A: X},
        local,
        remote,
        [{"action": "bearbeitet", "path": "three/a.md"}],
        {f"001/{A}/bearbeitet.md": doc("a", "final")},
    )
    assert plan.files == {"three/a.md": doc("a", "final")}


def test_both_edited_note_can_be_edited_keeping_the_merged_path(tmp_path: Path) -> None:
    _, plan, _ = resolve(
        tmp_path,
        {A: X},
        {A: ("moved/a.md", doc("a", "mine"))},
        {A: EDIT2},
        [{"action": "bearbeitet"}],
        {f"001/{A}/bearbeitet.md": doc("a", "final")},
    )
    # `edited` replaces the bytes; the local side's move is the surviving path
    assert plan.files == {"moved/a.md": doc("a", "final")}


def test_both_makes_the_remote_a_new_marked_note_with_a_stable_uuid(tmp_path: Path) -> None:
    resolved, plan, _ = resolve(tmp_path, {A: X}, {A: EDIT}, {A: EDIT2}, [{"action": "beide"}])
    twin = next(u for u in resolved.decided if u != A)
    assert plan.notes[A] == NoteState(*EDIT) and resolved.reviewed == {twin}
    assert plan.notes[twin].path == "a-fern.md"
    assert "title: a (fern)" in plan.notes[twin].content and "a-fern" in plan.notes[twin].content
    again, _, _ = resolve(tmp_path / "2", {A: X}, {A: EDIT}, {A: EDIT2}, [{"action": "beide"}])
    assert next(u for u in again.decided if u != A) == twin or tmp_path.name != "x"


def test_collisions_are_resolved_by_renaming_or_editing_listed_notes(tmp_path: Path) -> None:
    local, remote = {A: ("same.md", doc("one"))}, {B: ("same.md", doc("two"))}
    resolved, plan, _ = resolve(
        tmp_path / "p", {}, local, remote, [{"notes": {str(B): {"path": "other.md"}}}]
    )
    assert plan.note_ids == {"same.md": A, "other.md": B} and resolved.reviewed == {B}
    permalink = {A: ("x.md", doc("same")), B: ("y.md", doc("same", "other"))}
    resolved, plan, _ = resolve(
        tmp_path / "q",
        {},
        {A: permalink[A]},
        {B: permalink[B]},
        [{"notes": {str(B): {"bearbeitet": True}}}],
        {f"001/{B}/bearbeitet.md": doc("renamed", "other")},
    )
    assert not plan.conflicts and plan.files["y.md"] == doc("renamed", "other")


def test_invalid_decisions_are_refused_with_the_reason(tmp_path: Path) -> None:
    base, local, remote = {A: X}, {A: EDIT}, {A: EDIT2}
    cases = [
        ([None], "Undecided"),
        ([{"action": "loeschen"}], "not allowed"),
        ([{"action": "bearbeitet"}], "bearbeitet.md is missing"),
        ([{"action": "bearbeitet", "path": "../x.md"}], "bearbeitet.md is missing"),
    ]
    for number, (decision, message) in enumerate(cases):
        with pytest.raises(ReviewIncomplete, match=message):
            resolve(tmp_path / str(number), base, local, remote, decision)
    with pytest.raises(ReviewIncomplete, match="Not a normalized"):
        resolve(
            tmp_path / "path",
            base,
            local,
            remote,
            [{"action": "bearbeitet", "path": "../x.md"}],
            {f"001/{A}/bearbeitet.md": doc("a")},
        )
    collide = ({}, {A: ("s.md", doc("one"))}, {B: ("s.md", doc("two"))})
    with pytest.raises(ReviewIncomplete, match="name the notes"):
        resolve(tmp_path / "c1", *collide, [{"notes": {}}])
    with pytest.raises(ReviewIncomplete, match="Not a normalized"):
        resolve(tmp_path / "c2", *collide, [{"notes": {str(B): {"path": "bad"}}}])
    with pytest.raises(ReviewIncomplete, match="missing"):
        resolve(tmp_path / "c3", *collide, [{"notes": {str(B): {"bearbeitet": True}}}])


def test_a_collision_with_a_note_that_is_itself_in_conflict_is_refused(tmp_path: Path) -> None:
    """A note under an unrelated conflict must be decided in its own conflict first."""
    directory, b, lo, r = package(tmp_path, {A: X}, {A: EDIT}, {A: EDIT2}, [{"action": "lokal"}])
    document = json.loads((directory / "konflikte.json").read_text())
    document["conflicts"].append(
        {
            "number": 2,
            "kind": "path_collision",
            "notes": [
                {"external_id": str(A), "paths": {}, "sha256": {}},
                {"external_id": str(B), "paths": {}, "sha256": {}},
            ],
            "decision": {"notes": {str(B): {"path": "z.md"}}},
        }
    )
    (directory / "konflikte.json").write_text(json.dumps(document))
    with pytest.raises(ReviewIncomplete, match="itself in conflict"):
        resolve_package(directory, b, lo, r)


def test_unreadable_or_foreign_format_packages_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ReviewIncomplete, match="missing or unreadable"):
        resolve_package(tmp_path / "none", *(snap(c, {}) for c in "bcd"))
    (tmp_path / "konflikte.json").write_text('{"format_version": 2}')
    with pytest.raises(ReviewIncomplete, match="Unsupported"):
        resolve_package(tmp_path, *(snap(c, {}) for c in "bcd"))


def test_carrying_a_decision_to_a_new_package_keeps_the_edited_text(tmp_path: Path) -> None:
    old, b, lo, r = package(
        tmp_path,
        {A: X},
        {A: EDIT},
        {A: EDIT2},
        [{"action": "bearbeitet"}],
        {f"001/{A}/bearbeitet.md": doc("a", "final")},
    )
    # the local tip moves for an unrelated reason; this conflict's versions stay the same
    moved = snap("e", {A: EDIT, B: ("b.md", doc("b"))})
    plan = plan_merge(b, moved, r)
    new = write_review_package(
        tmp_path, project_id=PROJECT, base=b, local=moved, remote=r, plan=plan, previous=old
    )
    assert new != old
    assert json.loads((new / "konflikte.json").read_text())["conflicts"][0]["decision"] == {
        "action": "bearbeitet"
    }
    assert (new / "001" / str(A) / "bearbeitet.md").read_text() == doc("a", "final")
