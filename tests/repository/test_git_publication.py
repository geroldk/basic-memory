"""Real Git tests for the isolated publication/receipt boundary."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from basic_memory.repository.git_project_repository import (
    GitProjectRepository,
    GitPublicationConflict,
    GitPublicationError,
    PreparedGitPublication,
    validate_note_path,
)


def prepare(
    repository: GitProjectRepository,
    *,
    transaction_id: UUID,
    base_commit: str,
    files: dict[str, str],
) -> PreparedGitPublication:
    namespace = repository.snapshot(base_commit).manifest.project_external_id
    return repository.prepare(
        transaction_id=transaction_id,
        base_commit=base_commit,
        files=files,
        note_ids={path: uuid5(namespace, path) for path in files},
    )


@pytest.fixture
def repository(tmp_path: Path) -> GitProjectRepository:
    return GitProjectRepository.create(tmp_path / "project.git")


def test_prepare_is_private_and_publish_preserves_exact_bytes(
    repository: GitProjectRepository,
) -> None:
    base = repository.head()
    files = {"notes/Überblick mit Leerzeichen.md": "---\ntitle: Überblick\n---\n\nGrüße!\r\n"}
    prepared = prepare(repository, transaction_id=uuid4(), base_commit=base, files=files)
    assert repository.head() == base
    assert repository.snapshot().files == {}
    assert repository.transaction_status(prepared.transaction_id) is None
    with pytest.raises(GitPublicationError):
        repository.snapshot(prepared.commit_id)
    result = repository.publish(prepared)
    assert not result.already_committed
    assert repository.snapshot().files == files
    status = repository.transaction_status(prepared.transaction_id)
    assert status is not None
    assert status.commit_id == result.commit_id
    # A later published revision cannot change the immutable base of an older read.
    assert repository.snapshot(base).files == {}


def test_terminal_transaction_does_not_publish_again(repository: GitProjectRepository) -> None:
    base = repository.head()
    transaction_id = uuid4()
    first = prepare(
        repository, transaction_id=transaction_id, base_commit=base, files={"a.md": "one"}
    )
    published = repository.publish(first)
    second = prepare(
        repository, transaction_id=uuid4(), base_commit=published.commit_id, files={"a.md": "two"}
    )
    latest = repository.publish(second)
    # Retrying the old terminal transaction returns its original commit even when
    # the project advanced. It must not reset the branch to that older commit.
    replay = GitProjectRepository(repository.path).publish(first)
    assert replay.already_committed
    assert replay.commit_id == published.commit_id
    assert repository.head() == latest.commit_id
    assert repository.snapshot().files == {"a.md": "two"}


def test_no_op_has_a_terminal_receipt_without_an_empty_commit(
    repository: GitProjectRepository,
) -> None:
    base = repository.head()
    prepared = prepare(repository, transaction_id=uuid4(), base_commit=base, files={})
    assert not prepared.changed
    assert repository.publish(prepared).commit_id == base
    assert repository.publish(prepared).already_committed
    assert repository.head() == base


def test_same_project_conflicts_but_other_project_does_not(
    repository: GitProjectRepository, tmp_path: Path
) -> None:
    other = GitProjectRepository.create(tmp_path / "other.git")
    pending = prepare(
        repository, transaction_id=uuid4(), base_commit=repository.head(), files={"a.md": "draft"}
    )
    other.publish(
        prepare(other, transaction_id=uuid4(), base_commit=other.head(), files={"b.md": "B"})
    )
    repository.publish(pending)
    stale = prepare(
        repository,
        transaction_id=uuid4(),
        base_commit=pending.base_commit,
        files={"other.md": "stale"},
    )
    with pytest.raises(GitPublicationConflict) as conflict:
        repository.publish(stale)
    assert conflict.value.expected == pending.base_commit
    assert conflict.value.actual == pending.commit_id
    assert repository.transaction_status(stale.transaction_id) is None
    assert repository.snapshot().files == {"a.md": "draft"}


def test_competing_publishers_have_exactly_one_winner(repository: GitProjectRepository) -> None:
    base = repository.head()
    prepared = [
        prepare(repository, transaction_id=uuid4(), base_commit=base, files={"a.md": str(number)})
        for number in range(2)
    ]

    def publish(number: int) -> bool:
        try:
            GitProjectRepository(repository.path).publish(prepared[number])
            return True
        except GitPublicationConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sorted(results) == [False, True]
    assert (
        sum(repository.transaction_status(item.transaction_id) is not None for item in prepared)
        == 1
    )


@pytest.mark.parametrize("publish", [False, True])
def test_process_exit_around_publication_is_recoverable(
    repository: GitProjectRepository, publish: bool
) -> None:
    transaction_id = uuid4()
    base = repository.head()
    program = """
import os, sys
from pathlib import Path
from uuid import UUID
from basic_memory.repository.git_project_repository import GitProjectRepository
repository = GitProjectRepository(Path(sys.argv[1]))
prepared = repository.prepare(transaction_id=UUID(sys.argv[2]), base_commit=sys.argv[3], files={'a.md': 'durable'}, note_ids={'a.md': UUID(sys.argv[2])})
if sys.argv[4] == 'True':
    repository.publish(prepared)
os._exit(73)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(repository.path),
            str(transaction_id),
            base,
            str(publish),
        ],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        timeout=30,
    )
    assert result.returncode == 73, result.stderr.decode()
    reopened = GitProjectRepository(repository.path)
    status = reopened.transaction_status(transaction_id)
    assert (status is not None) == publish
    assert reopened.snapshot().files == ({"a.md": "durable"} if publish else {})
    if status is not None:
        assert status.commit_id == reopened.head()
    else:
        assert reopened.head() == base


@pytest.mark.parametrize(
    "path",
    [
        "/a.md",
        "../a.md",
        "x/../a.md",
        "a//b.md",
        ".git/a.md",
        "x/.GIT/a.md",
        "x\\a.md",
        "C:a.md",
        "a.txt",
        "a\nb.md",
        "./a.md",
        ".basic-memory/extra.md",
        "x/.BASIC-MEMORY/extra.md",
    ],
)
def test_control_paths_and_non_markdown_are_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        validate_note_path(path)


def test_existing_directory_is_never_adopted(tmp_path: Path) -> None:
    note = tmp_path / "private.md"
    note.write_text("private")
    with pytest.raises(FileExistsError):
        GitProjectRepository.create(tmp_path)
    assert note.read_text() == "private"


def test_callers_git_environment_cannot_redirect_writes(
    repository: GitProjectRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = GitProjectRepository.create(tmp_path / "unrelated.git")
    other_base = other.head()
    monkeypatch.setenv("GIT_DIR", str(other.path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "user-index"))
    prepared = prepare(
        repository, transaction_id=uuid4(), base_commit=repository.head(), files={"a.md": "one"}
    )
    repository.publish(prepared)
    assert other.head() == other_base
    assert not (tmp_path / "user-index").exists()


def test_stale_no_op_cannot_close_as_success(repository: GitProjectRepository) -> None:
    base = repository.head()
    unchanged = prepare(repository, transaction_id=uuid4(), base_commit=base, files={})
    repository.publish(
        prepare(repository, transaction_id=uuid4(), base_commit=base, files={"a.md": "new"})
    )
    with pytest.raises(GitPublicationConflict):
        repository.publish(unchanged)
    assert repository.transaction_status(unchanged.transaction_id) is None


@pytest.mark.parametrize("no_op_first", [True, False])
def test_terminal_no_op_and_effective_candidate_cannot_both_publish(
    repository: GitProjectRepository, no_op_first: bool
) -> None:
    transaction_id = uuid4()
    base = repository.head()
    unchanged = prepare(repository, transaction_id=transaction_id, base_commit=base, files={})
    changed = prepare(
        repository, transaction_id=transaction_id, base_commit=base, files={"a.md": "new"}
    )
    first, second = (unchanged, changed) if no_op_first else (changed, unchanged)
    result = repository.publish(first)
    replay = repository.publish(second)
    assert replay.already_committed
    assert result.commit_id == replay.commit_id == repository.head()
    assert repository.snapshot().files == ({} if no_op_first else {"a.md": "new"})


def test_a_git_lock_error_never_reports_acceptance(repository: GitProjectRepository) -> None:
    base = repository.head()
    prepared = prepare(repository, transaction_id=uuid4(), base_commit=base, files={"a.md": "new"})
    lock = repository.path / "refs/heads/main.lock"
    lock.write_bytes(b"held by another process")
    with pytest.raises(GitPublicationError):
        repository.publish(prepared)
    assert repository.head() == base
    assert repository.transaction_status(prepared.transaction_id) is None
    lock.unlink()
    assert repository.publish(prepared).commit_id == prepared.commit_id


def test_repository_disappearance_is_not_an_unknown_transaction(
    repository: GitProjectRepository,
) -> None:
    repository.path.rename(repository.path.with_name("moved.git"))
    with pytest.raises(GitPublicationError):
        repository.transaction_status(uuid4())


def test_working_repository_is_not_a_publication_store(tmp_path: Path) -> None:
    working = tmp_path / "working"
    subprocess.run(["git", "init", str(working)], check=True, capture_output=True)
    with pytest.raises(ValueError, match="bare"):
        GitProjectRepository(working / ".git")


def test_invalid_commit_ids_fail_before_revision_resolution(
    repository: GitProjectRepository,
) -> None:
    with pytest.raises(ValueError, match="commit ID"):
        repository.snapshot("HEAD~1")


def test_snapshot_rejects_a_published_symlink(repository: GitProjectRepository) -> None:
    blob = (
        repository._git("hash-object", "-w", "--stdin", input_bytes=b"/private/note.md")
        .strip()
        .decode()
    )
    tree = (
        repository._git("mktree", input_bytes=f"120000 blob {blob}\tleak.md\n".encode())
        .strip()
        .decode()
    )
    base = repository.head()
    commit = (
        repository._git("commit-tree", tree, "-p", base, input_bytes=b"Unsupported import\n")
        .strip()
        .decode()
    )
    repository._git("update-ref", "refs/heads/main", commit, base)
    with pytest.raises(GitPublicationError, match="non-regular"):
        repository.snapshot()


def test_initialization_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=b"injected disk failure")

    monkeypatch.setattr(subprocess, "run", fail_git)
    with pytest.raises(GitPublicationError, match="disk failure"):
        GitProjectRepository.create(tmp_path / "failed.git")


def test_competing_completion_between_status_check_and_cas_returns_original_sha(
    repository: GitProjectRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction_id = uuid4()
    base = repository.head()
    winner = prepare(
        repository, transaction_id=transaction_id, base_commit=base, files={"a.md": "winner"}
    )
    loser = prepare(
        repository, transaction_id=transaction_id, base_commit=base, files={"a.md": "loser"}
    )
    run = repository._run
    competitor = GitProjectRepository(repository.path)

    def complete_first(
        *arguments: str, input_bytes: bytes | None = None, index_path: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        if arguments == ("update-ref", "--stdin"):
            competitor.publish(winner)
        return run(*arguments, input_bytes=input_bytes, index_path=index_path)

    monkeypatch.setattr(repository, "_run", complete_first)
    result = repository.publish(loser)
    assert result.already_committed
    assert result.commit_id == winner.commit_id == competitor.head()
    assert competitor.snapshot().files == {"a.md": "winner"}


def test_manifest_and_notes_travel_together_to_an_independent_clone(
    repository: GitProjectRepository, tmp_path: Path
) -> None:
    note_id = uuid4()
    published = repository.publish(
        repository.prepare(
            transaction_id=uuid4(),
            base_commit=repository.head(),
            files={"a.md": "canonical"},
            note_ids={"a.md": note_id},
        )
    )
    repository.prepare(
        transaction_id=uuid4(),
        base_commit=published.commit_id,
        files={"a.md": "private draft"},
        note_ids={"a.md": note_id},
    )
    clone = tmp_path / "clone.git"
    subprocess.run(
        ["git", "clone", "--bare", "--no-local", str(repository.path), str(clone)],
        check=True,
        capture_output=True,
    )
    snapshot = GitProjectRepository(clone).snapshot()
    assert snapshot.commit_id == published.commit_id
    assert snapshot.files == {"a.md": "canonical"}
    assert snapshot.manifest.note_ids == {"a.md": note_id}
    assert (
        snapshot.manifest.project_external_id == repository.snapshot().manifest.project_external_id
    )


def test_identity_and_file_sets_must_agree(repository: GitProjectRepository) -> None:
    with pytest.raises(ValueError, match="explicit note identity"):
        repository.prepare(
            transaction_id=uuid4(),
            base_commit=repository.head(),
            files={"a.md": "body"},
            note_ids={},
        )
    first = prepare(
        repository, transaction_id=uuid4(), base_commit=repository.head(), files={"a.md": "old"}
    )
    repository.publish(first)
    with pytest.raises(ValueError, match="different identity"):
        repository.prepare(
            transaction_id=uuid4(),
            base_commit=first.commit_id,
            files={"a.md": "new"},
            note_ids={"a.md": uuid4()},
        )


def test_unknown_file_or_missing_manifest_is_not_a_valid_snapshot(
    repository: GitProjectRepository,
) -> None:
    base = repository.head()
    tree = repository._write_tree({"a.md": "unidentified"})
    commit = (
        repository._git("commit-tree", tree, "-p", base, input_bytes=b"Invalid import\n")
        .strip()
        .decode()
    )
    repository._git("update-ref", "refs/heads/main", commit, base)
    with pytest.raises(GitPublicationError, match="identify every"):
        repository.snapshot()


def test_only_published_history_can_authorize_older_sql_content(
    repository: GitProjectRepository,
) -> None:
    first = prepare(
        repository, transaction_id=uuid4(), base_commit=repository.head(), files={"a.md": "old"}
    )
    repository.publish(first)
    second = prepare(
        repository, transaction_id=uuid4(), base_commit=first.commit_id, files={"a.md": "current"}
    )
    repository.publish(second)
    prepare(
        repository, transaction_id=uuid4(), base_commit=second.commit_id, files={"a.md": "private"}
    )
    assert repository.contains_published_note_content(second.commit_id, "a.md", "old")
    assert repository.contains_published_note_content(second.commit_id, "a.md", "current")
    assert not repository.contains_published_note_content(second.commit_id, "a.md", "private")
    assert not repository.contains_published_note_content(second.commit_id, "other.md", "old")
