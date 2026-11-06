"""Identity proofs must survive merge commits, whichever parent carries the evidence."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.schemas.git_project import (
    GIT_PROJECT_MANIFEST_PATH,
    GitNoteIdentity,
    GitProjectManifest,
)


def commit(
    repository: GitProjectRepository,
    parents: list[str],
    notes: dict[str, tuple[UUID, str]],
) -> str:
    """Hand-build a commit; `notes` maps path to (uuid, bytes)."""
    manifest = GitProjectManifest(
        project_external_id=repository.snapshot().manifest.project_external_id,
        notes=tuple(GitNoteIdentity(path=p, external_id=i) for p, (i, _) in notes.items()),
    )
    tree = repository._write_tree(
        {
            **{path: content for path, (_, content) in notes.items()},
            GIT_PROJECT_MANIFEST_PATH: manifest.canonical_json(),
        }
    )
    arguments = [argument for parent in parents for argument in ("-p", parent)]
    return (
        repository._git("commit-tree", tree, *arguments, input_bytes=b"test\n").strip().decode()
    )


@pytest.fixture
def repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository.create(tmp_path / "project.git")


def test_identity_is_proven_through_the_second_parent_of_a_merge(
    repository: GitProjectRepository,
) -> None:
    """One side edits the bytes, the other moves the note: the merge is TREESAME to the mover.

    Default history simplification would follow only the mover and lose the editor's bytes,
    so the editor's own host could not prove its local bytes after the merge.
    """
    note = uuid4()
    base = commit(repository, [repository.head()], {"a.md": (note, "old")})
    edited = commit(repository, [base], {"a.md": (note, "edited")})
    moved = commit(repository, [base], {"b.md": (note, "old")})
    merged = commit(repository, [moved, edited], {"b.md": (note, "edited")})
    repository._git("update-ref", "refs/heads/main", merged)
    # The mover is the first parent, so the editor's evidence is reachable only through parent two.
    assert repository.contains_published_note_identity(merged, note, "a.md", "edited")
    assert repository.contains_published_note_identity(merged, note, "a.md", "old")
    assert repository.contains_published_note_identity(merged, note, "b.md", "old")
    assert not repository.contains_published_note_identity(merged, note, "a.md", "unrelated")
    assert not repository.contains_published_note_identity(merged, uuid4(), "a.md", "edited")
