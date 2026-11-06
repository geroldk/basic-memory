"""prepare_merge / publish_merge: two parents, provenance, one guarded ref update."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from basic_memory.repository.git_project_repository import (
    GitProjectRepository,
    GitPublicationConflict,
    GitPublicationError,
)
from basic_memory.schemas.git_project import (
    GIT_PROJECT_MANIFEST_PATH,
    GitNoteIdentity,
    GitProjectManifest,
)

A, B = UUID(int=1), UUID(int=2)


def commit(repository: GitProjectRepository, parents: list[str], notes) -> str:
    manifest = GitProjectManifest(
        project_external_id=repository.snapshot().manifest.project_external_id,
        notes=tuple(GitNoteIdentity(path=p, external_id=i) for p, (i, _) in notes.items()),
    )
    tree = repository._write_tree(
        {
            **{p: c for p, (_, c) in notes.items()},
            GIT_PROJECT_MANIFEST_PATH: manifest.canonical_json(),
        }
    )
    arguments = [argument for parent in parents for argument in ("-p", parent)]
    return repository._git("commit-tree", tree, *arguments, input_bytes=b"t\n").strip().decode()


@pytest.fixture
def repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository.create(tmp_path / "project.git")


@pytest.fixture
def forks(repository):
    """base -> local (adds b.md) and base -> remote (adds a2.md)."""
    base = commit(repository, [repository.head()], {"a.md": (A, "a")})
    local = commit(repository, [base], {"a.md": (A, "a"), "b.md": (B, "b")})
    remote = commit(repository, [base], {"a.md": (A, "a"), "c.md": (uuid4(), "c")})
    repository._git("update-ref", "refs/heads/main", local)
    attempt = uuid4()
    repository._git("update-ref", f"refs/bm/incoming/{attempt}", remote)
    return base, local, remote, attempt


def snaps(repository, *commits):
    return [repository.read_snapshot(c) for c in commits]


def merged_notes(repository, local, remote):
    left, right = snaps(repository, local, remote)
    files = {**left.files, **right.files}
    ids = {**left.manifest.note_ids, **right.manifest.note_ids}
    return files, ids


def prepare(repository, forks, **overrides):
    base, local, remote, attempt = forks
    base_s, local_s, remote_s = snaps(repository, base, local, remote)
    files, ids = merged_notes(repository, local, remote)
    arguments = dict(
        attempt_id=attempt, base=base_s, local=local_s, remote=remote_s, files=files, note_ids=ids
    )
    arguments.update(overrides)
    return repository.prepare_merge(**arguments)


def test_merge_bases_reports_one_base_and_many_for_criss_cross(repository, forks) -> None:
    base, local, remote, _ = forks
    assert repository.merge_bases(local, remote) == [base]
    cross_a = commit(repository, [local, remote], {"a.md": (A, "a")})
    cross_b = commit(repository, [remote, local], {"a.md": (A, "a")})
    one = commit(repository, [cross_a], {"a.md": (A, "1")})
    two = commit(repository, [cross_b], {"a.md": (A, "2")})
    assert len(repository.merge_bases(one, two)) == 2


def test_prepare_merge_builds_two_parents_with_trailers_and_publishes(repository, forks) -> None:
    base, local, remote, attempt = forks
    prepared = prepare(repository, forks)
    assert repository.head() == local  # nothing published yet
    parents = repository._git("rev-list", "--parents", "-n", "1", prepared.commit_id).split()
    assert [p.decode() for p in parents[1:]] == [local, remote]
    message = repository._git("show", "--no-patch", "--format=%B", prepared.commit_id).decode()
    assert f"Basic-Memory-Merge: {attempt}" in message
    assert f"Basic-Memory-Merge-Base: {base}" in message
    repository.publish_merge(prepared)
    assert repository.head() == prepared.commit_id
    assert set(repository.snapshot().files) == {"a.md", "b.md", "c.md"}
    assert repository.is_ancestor(local, prepared.commit_id)
    assert repository.is_ancestor(remote, prepared.commit_id)


def test_review_trailer_is_written_when_a_person_decided(repository, forks) -> None:
    review = uuid4()
    prepared = prepare(repository, forks, review_id=review)
    message = repository._git("show", "--no-patch", "--format=%B", prepared.commit_id).decode()
    assert f"Basic-Memory-Review: {review}" in message


def test_publish_merge_is_idempotent_after_a_lost_answer(repository, forks) -> None:
    prepared = prepare(repository, forks)
    repository.publish_merge(prepared)
    repository.publish_merge(prepared)
    assert repository.head() == prepared.commit_id


def test_publish_merge_refuses_when_local_main_moved(repository, forks) -> None:
    prepared = prepare(repository, forks)
    repository._git("update-ref", "refs/heads/main", forks[0], forks[1])  # main moved away
    with pytest.raises(GitPublicationConflict):
        repository.publish_merge(prepared)
    assert repository.head() == forks[0]


def test_publish_merge_refuses_when_incoming_ref_changed(repository, forks) -> None:
    prepared = prepare(repository, forks)
    repository._git("update-ref", f"refs/bm/incoming/{forks[3]}", forks[1])
    with pytest.raises(GitPublicationError, match="Incoming reference changed"):
        repository.publish_merge(prepared)
    assert repository.head() == forks[1]


def test_publish_merge_refuses_a_commit_with_other_parents(repository, forks) -> None:
    base, local, remote, attempt = forks
    prepared = prepare(repository, forks)
    stray = commit(repository, [local], {"a.md": (A, "a")})
    wrong = prepared.__class__(attempt, base, local, remote, stray)
    with pytest.raises(GitPublicationError, match="expected parents"):
        repository.publish_merge(wrong)


def test_a_note_that_comes_from_neither_parent_is_refused_unless_reviewed(
    repository, forks
) -> None:
    files, ids = merged_notes(repository, forks[1], forks[2])
    files = {**files, "b.md": "invented"}
    with pytest.raises(ValueError, match="does not come from either parent"):
        prepare(repository, forks, files=files, note_ids=ids)
    prepare(repository, forks, files=files, note_ids=ids, reviewed=frozenset({B}))


def test_a_path_cannot_silently_get_a_different_owner(repository, forks) -> None:
    files, ids = merged_notes(repository, forks[1], forks[2])
    stolen = {**ids, "a.md": B}
    del stolen["b.md"]
    files = {p: c for p, c in files.items() if p != "b.md"}
    with pytest.raises(ValueError, match="silently acquire"):
        prepare(repository, forks, files=files, note_ids=stolen, reviewed=frozenset({A, B}))


def test_inconsistent_inputs_are_refused(repository, forks) -> None:
    files, ids = merged_notes(repository, forks[1], forks[2])
    with pytest.raises(ValueError, match="explicit note identity"):
        prepare(repository, forks, files=files, note_ids={})
    base_s, local_s, _ = snaps(repository, forks[0], forks[1], forks[2])
    with pytest.raises(ValueError, match="two distinct tips"):
        prepare(repository, forks, remote=local_s)
    with pytest.raises(ValueError, match="two distinct tips"):
        prepare(repository, forks, base=local_s)
    other = GitProjectRepository.create(repository.path.parent / "other.git")
    foreign = other.snapshot()
    foreign_remote = type(local_s)(forks[2], foreign.files, foreign.manifest)
    with pytest.raises(ValueError, match="different projects"):
        prepare(repository, forks, remote=foreign_remote)
    foreign_base = type(local_s)(forks[0], foreign.files, foreign.manifest)
    with pytest.raises(ValueError, match="different projects"):
        prepare(repository, forks, base=foreign_base)


def test_transaction_messages_may_not_forge_any_basic_memory_trailer(repository) -> None:
    for line in ("Basic-Memory-Transaction: x", "Basic-Memory-Merge: x", "Basic-Memory-Review: x"):
        with pytest.raises(ValueError, match="reserved"):
            repository.prepare(
                transaction_id=uuid4(),
                base_commit=repository.head(),
                files={},
                note_ids={},
                message=f"subject\n\n{line}",
            )
