"""The case table of the per-note merge, one test per row (no Git, no I/O)."""

from uuid import UUID, uuid4

from basic_memory.repository.git_project_repository import GitSnapshot
from basic_memory.schemas.git_project import GitNoteIdentity, GitProjectManifest
from basic_memory.services.git_merge import NoteState, plan_merge

PROJECT = uuid4()
A, B = UUID(int=1), UUID(int=2)


def doc(permalink: str, body: str = "x") -> str:
    return f"---\ntitle: {permalink}\ntype: note\npermalink: {permalink}\n---\n{body}\n"


def snap(notes: dict[UUID, tuple[str, str]]) -> GitSnapshot:
    manifest = GitProjectManifest(
        project_external_id=PROJECT,
        notes=tuple(GitNoteIdentity(path=p, external_id=i) for i, (p, _) in notes.items()),
    )
    return GitSnapshot("0" * 40, {p: c for p, c in notes.values()}, manifest)


def merge(base, local, remote):
    return plan_merge(snap(base), snap(local), snap(remote))


def kinds(plan):
    return sorted(c.kind for c in plan.conflicts)


X = ("a.md", doc("a"))
X_EDIT = ("a.md", doc("a", "edited"))
X_MOVED = ("moved/a.md", doc("a"))
X_MOVED2 = ("other/a.md", doc("a"))
X_EDIT2 = ("a.md", doc("a", "edited differently"))


def test_case_1_remote_changed_moved_or_deleted() -> None:
    assert merge({A: X}, {A: X}, {A: X_EDIT}).notes[A] == NoteState(*X_EDIT)
    assert merge({A: X}, {A: X}, {A: X_MOVED}).notes[A] == NoteState(*X_MOVED)
    assert A not in merge({A: X}, {A: X}, {}).notes


def test_case_2_local_changed() -> None:
    assert merge({A: X}, {A: X_EDIT}, {A: X}).notes[A] == NoteState(*X_EDIT)
    assert A not in merge({A: X}, {}, {A: X}).notes


def test_case_3_both_did_the_same_even_deleting() -> None:
    assert merge({A: X}, {A: X_EDIT}, {A: X_EDIT}).notes[A] == NoteState(*X_EDIT)
    plan = merge({A: X}, {}, {})
    assert plan.notes == {} and not plan.conflicts
    both_new = merge({}, {A: X}, {A: X})
    assert both_new.notes[A] == NoteState(*X)


def test_case_4_added_on_one_side_only() -> None:
    assert merge({}, {A: X}, {}).notes[A] == NoteState(*X)
    assert merge({}, {}, {A: X}).notes[A] == NoteState(*X)
    both = merge({}, {A: X}, {B: ("b.md", doc("b"))})
    assert set(both.notes) == {A, B} and both.files["b.md"] == doc("b")


def test_case_5_move_on_one_side_edit_on_the_other_in_both_directions() -> None:
    expected = NoteState("moved/a.md", doc("a", "edited"))
    assert merge({A: X}, {A: X_MOVED}, {A: X_EDIT}).notes[A] == expected
    assert merge({A: X}, {A: X_EDIT}, {A: X_MOVED}).notes[A] == expected


def test_case_6_both_changed_the_same_note_differently() -> None:
    plan = merge({A: X}, {A: X_EDIT}, {A: X_EDIT2})
    assert kinds(plan) == ["both_changed"] and plan.notes == {}
    assert plan.conflicts[0].external_ids == (A,)
    assert dict(plan.conflicts[0].paths) == {"base": "a.md", "local": "a.md", "remote": "a.md"}


def test_case_6_same_uuid_added_twice_with_different_bytes() -> None:
    assert kinds(merge({}, {A: X}, {A: X_EDIT})) == ["both_changed"]


def test_case_7_deleted_against_changed_either_way() -> None:
    assert kinds(merge({A: X}, {}, {A: X_EDIT})) == ["delete_vs_change"]
    assert kinds(merge({A: X}, {A: X_MOVED}, {})) == ["delete_vs_change"]


def test_case_8_moved_to_two_different_places() -> None:
    assert kinds(merge({A: X}, {A: X_MOVED}, {A: X_MOVED2})) == ["moved_twice"]


def test_both_moved_to_the_same_place_is_not_a_conflict() -> None:
    assert merge({A: X}, {A: X_MOVED}, {A: X_MOVED}).notes[A] == NoteState(*X_MOVED)


def test_case_9_two_notes_claim_one_path() -> None:
    plan = merge({}, {A: ("same.md", doc("one"))}, {B: ("same.md", doc("two"))})
    assert kinds(plan) == ["path_collision"] and plan.conflicts[0].external_ids == (A, B)


def test_case_10_two_notes_share_a_permalink() -> None:
    plan = merge({}, {A: ("x.md", doc("same"))}, {B: ("y.md", doc("same"))})
    assert kinds(plan) == ["permalink_collision"]


def test_case_11_a_path_may_change_owner_when_the_other_side_vacated_it() -> None:
    """Remote swaps two notes' paths; the result stays a legal permutation."""
    base = {A: ("a.md", doc("a")), B: ("b.md", doc("b"))}
    remote = {A: ("b.md", doc("a")), B: ("a.md", doc("b"))}
    plan = merge(base, base, remote)
    assert plan.note_ids == {"b.md": A, "a.md": B}


def test_case_12_links_to_deleted_notes_do_not_block() -> None:
    linking = ("l.md", doc("l", "[[Gone]]"))
    plan = merge({A: X, B: linking}, {B: linking}, {A: X, B: linking})
    assert A not in plan.notes and B in plan.notes and not plan.conflicts


def test_unparseable_frontmatter_has_no_permalink_so_never_collides() -> None:
    broken = ("z.md", "no frontmatter at all")
    plan = merge({}, {A: broken}, {B: ("y.md", "also none")})
    assert not plan.conflicts and set(plan.notes) == {A, B}


def test_unconflicted_notes_are_still_withheld_while_any_conflict_remains() -> None:
    plan = merge(
        {A: X, B: ("b.md", doc("b"))},
        {A: X_EDIT, B: ("b.md", doc("b", "mine"))},
        {A: X_EDIT2, B: ("b.md", doc("b"))},
    )
    assert plan.notes == {} and kinds(plan) == ["both_changed"]


def test_result_is_independent_of_dict_order() -> None:
    first = merge({}, {A: X, B: ("b.md", doc("b"))}, {})
    second = merge({}, {B: ("b.md", doc("b")), A: X}, {})
    assert first.files == second.files and first.note_ids == second.note_ids


def test_review_helpers_survive_a_failing_merge_file_and_a_missing_package(
    monkeypatch, tmp_path
) -> None:
    import subprocess

    import pytest

    from basic_memory.services import git_merge_review as review

    monkeypatch.setattr(
        review.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 255, b"", b""),
    )
    with pytest.raises(OSError, match="merge-file"):
        review._proposal("b", "l", "r")
    assert review.read_review(tmp_path, "missing") is None
