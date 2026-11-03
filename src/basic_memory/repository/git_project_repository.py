"""Isolated Git publication primitive for BM-006; not enabled by the local runtime.

The published branch is authoritative. Private candidates never change that branch.
A changed transaction's terminal evidence is in the published commit itself, so the
single branch update publishes bytes and evidence together. SQLite stays outside.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import UUID

PUBLISHED_REF = "refs/heads/main"
_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z")


class GitPublicationError(RuntimeError):
    """A Git operation failed without a classified publication outcome."""


class GitPublicationConflict(GitPublicationError):
    """The project advanced after the candidate's fixed basis was captured."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"Project advanced from {expected} to {actual}")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    commit_id: str
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PreparedGitPublication:
    transaction_id: UUID
    base_commit: str
    commit_id: str
    candidate_ref: str
    changed: bool


@dataclass(frozen=True, slots=True)
class GitPublication:
    commit_id: str
    already_committed: bool


def validate_note_path(path: str) -> None:
    """Accept only normalized relative Markdown paths, never Git control files."""
    parts = path.split("/")
    if (
        not path
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or PurePosixPath(path).suffix.casefold() != ".md"
        or ":" in path
    ):
        raise ValueError(f"Not a normalized project Markdown path: {path!r}")


def _require_commit_id(commit_id: str) -> None:
    if _COMMIT_ID.fullmatch(commit_id) is None:
        raise ValueError("Expected a full SHA-1 Git commit ID")


class GitProjectRepository:
    """Own one dedicated bare project repository, independent of working files.

    No global Git configuration, hooks, shell, checkout, or user index participates.
    This first slice stores Markdown only; portable identity metadata comes later.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if self._git("rev-parse", "--is-bare-repository").strip() != b"true":
            raise ValueError("Git publication requires a dedicated bare project repository")

    @classmethod
    def create(cls, path: Path) -> GitProjectRepository:
        # Refuse an existing directory: this primitive must never convert a user's
        # note directory or implementation checkout into a publication repository.
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
        result = subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(path.resolve())],
            capture_output=True,
            check=False,
            env=cls._environment(),
            timeout=30,
        )
        if result.returncode != 0:
            raise GitPublicationError(result.stderr.decode("utf-8", errors="replace"))
        repository = cls(path)
        empty_tree = repository._git("mktree", input_bytes=b"").strip().decode("ascii")
        initial = (
            repository._git(
                "commit-tree", empty_tree, input_bytes=b"Initialize Basic Memory project\n"
            )
            .strip()
            .decode("ascii")
        )
        repository._git("update-ref", PUBLISHED_REF, initial, "0" * 40)
        return repository

    @staticmethod
    def _environment(index_path: Path | None = None) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="Basic Memory",
            GIT_AUTHOR_EMAIL="basic-memory@localhost",
            GIT_COMMITTER_NAME="Basic Memory",
            GIT_COMMITTER_EMAIL="basic-memory@localhost",
        )
        if index_path is not None:
            environment["GIT_INDEX_FILE"] = str(index_path)
        return environment

    def _run(
        self, *arguments: str, input_bytes: bytes | None = None, index_path: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "--no-replace-objects", "--git-dir", str(self.path), *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
            env=self._environment(index_path),
            timeout=30,
        )

    def _git(
        self, *arguments: str, input_bytes: bytes | None = None, index_path: Path | None = None
    ) -> bytes:
        result = self._run(*arguments, input_bytes=input_bytes, index_path=index_path)
        if result.returncode != 0:
            raise GitPublicationError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    def head(self) -> str:
        return self._git("rev-parse", "--verify", PUBLISHED_REF).strip().decode("ascii")

    def transaction_status(self, transaction_id: UUID) -> GitPublication | None:
        result = self._run("rev-parse", "--verify", "--quiet", f"refs/bm/receipts/{transaction_id}")
        if result.returncode not in {0, 1}:
            raise GitPublicationError(result.stderr.decode("utf-8", errors="replace"))
        if result.returncode == 0:
            return GitPublication(result.stdout.strip().decode("ascii"), already_committed=True)
        # Do not rely on a second ref being written atomically with the branch:
        # Git's files backend exposes individual ref changes separately. The
        # published history itself is the durable evidence for effective writes.
        commit_id = (
            self._git(
                "log",
                "--format=%H",
                "--max-count=1",
                "--fixed-strings",
                f"--grep=Basic-Memory-Transaction: {transaction_id}",
                PUBLISHED_REF,
                "--",
            )
            .strip()
            .decode("ascii")
        )
        return GitPublication(commit_id, already_committed=True) if commit_id else None

    def snapshot(self, commit_id: str | None = None) -> GitSnapshot:
        """Read immutable published bytes; this is internal, not the V2 read API."""
        commit_id = self.head() if commit_id is None else commit_id
        _require_commit_id(commit_id)
        self._git("merge-base", "--is-ancestor", commit_id, PUBLISHED_REF)
        tree = self._git("ls-tree", "-r", "-z", "--full-tree", commit_id)
        files: dict[str, str] = {}
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, blob_id = metadata.split(b" ")
            if mode != b"100644" or kind != b"blob":
                raise GitPublicationError("Published project contains a non-regular note")
            path = raw_path.decode("utf-8")
            validate_note_path(path)
            files[path] = self._git("cat-file", "blob", blob_id.decode("ascii")).decode("utf-8")
        return GitSnapshot(commit_id, files)

    def prepare(
        self, *, transaction_id: UUID, base_commit: str, files: Mapping[str, str]
    ) -> PreparedGitPublication:
        """Prepare a complete candidate without publishing bytes or holding a lock."""
        _require_commit_id(base_commit)
        self._git("merge-base", "--is-ancestor", base_commit, PUBLISHED_REF)
        for path in files:
            validate_note_path(path)
        with TemporaryDirectory(prefix="bm-git-index-") as directory:
            index_path = Path(directory) / "index"
            self._git("read-tree", "--empty", index_path=index_path)
            entries = bytearray()
            for path, content in sorted(files.items()):
                blob = self._git(
                    "hash-object", "-w", "--stdin", input_bytes=content.encode("utf-8")
                )
                entries.extend(b"100644 " + blob.strip() + b"\t" + path.encode("utf-8") + b"\0")
            self._git(
                "update-index",
                "-z",
                "--index-info",
                input_bytes=bytes(entries),
                index_path=index_path,
            )
            tree = self._git("write-tree", index_path=index_path).strip().decode("ascii")
        base_tree = self._git("rev-parse", f"{base_commit}^{{tree}}").strip().decode("ascii")
        changed = tree != base_tree
        commit_id = (
            self._git(
                "commit-tree",
                tree,
                "-p",
                base_commit,
                input_bytes=(
                    f"Publish Basic Memory notes\n\nBasic-Memory-Transaction: {transaction_id}\n"
                ).encode(),
            )
            .strip()
            .decode("ascii")
            if changed
            else base_commit
        )
        candidate_ref = f"refs/bm/candidates/{transaction_id}/{commit_id}"
        # Keep an interrupted candidate reachable for inspection; it is never read
        # by normal consumers and is not sent by an ordinary branch push.
        self._git("update-ref", candidate_ref, commit_id)
        return PreparedGitPublication(
            transaction_id, base_commit, commit_id, candidate_ref, changed
        )

    def publish(self, prepared: PreparedGitPublication) -> GitPublication:
        """Publish through one modified ref, guarded by the fixed project basis."""
        previous = self.transaction_status(prepared.transaction_id)
        if previous is not None:
            return previous
        receipt_ref = f"refs/bm/receipts/{prepared.transaction_id}"
        if prepared.changed:
            commands = (
                "start\n"
                f"verify {receipt_ref} {'0' * 40}\n"
                f"update {PUBLISHED_REF} {prepared.commit_id} {prepared.base_commit}\n"
                "prepare\ncommit\n"
            )
        else:
            # A no-op modifies only its receipt; checking/locking the branch and
            # the same receipt also serializes a competing effective publication
            # of this transaction. There is still only one modified ref.
            commands = (
                "start\n"
                f"verify {PUBLISHED_REF} {prepared.base_commit}\n"
                f"create {receipt_ref} {prepared.base_commit}\n"
                "prepare\ncommit\n"
            )
        result = self._run("update-ref", "--stdin", input_bytes=commands.encode("ascii"))
        if result.returncode == 0:
            return GitPublication(prepared.commit_id, already_committed=False)
        # A lost answer or a competing commit of this same explicit transaction
        # is resolved before classifying an ordinary stale-basis conflict.
        previous = self.transaction_status(prepared.transaction_id)
        if previous is not None:
            return previous
        actual = self.head()
        if actual != prepared.base_commit:
            raise GitPublicationConflict(prepared.base_commit, actual)
        raise GitPublicationError(result.stderr.decode("utf-8", errors="replace"))
