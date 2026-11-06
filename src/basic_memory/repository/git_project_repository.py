"""Git publication primitives for the opt-in local BM-006 runtime.

The published branch is authoritative. Private candidates never change that branch.
A changed transaction's terminal evidence is in the published commit itself, so the
single branch update publishes bytes and evidence together. SQLite stays outside.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from functools import partial
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID, uuid4

from basic_memory.schemas.git_project import (
    GIT_PROJECT_MANIFEST_PATH,
    GitNoteIdentity,
    GitProjectManifest,
    validate_note_path,
)

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
    manifest: GitProjectManifest


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


def _require_commit_id(commit_id: str) -> None:
    if _COMMIT_ID.fullmatch(commit_id) is None:
        raise ValueError("Expected a full SHA-1 Git commit ID")


async def settle_git_publication(
    repository: GitProjectRepository, prepared: PreparedGitPublication
) -> GitPublication:
    """Settle publication before cancellation can release a local ownership gate.

    The caller still receives cancellation and must recover from durable Git.
    Repeated cancellation never detaches a thread that can still publish.
    """
    return await settle_git_operation(partial(repository.publish, prepared))


async def settle_git_operation[T](operation: Callable[[], T]) -> T:
    """Finish a short filesystem mutation before its caller releases ownership."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        task.exception()
        raise cancelled
    return task.result()


class GitProjectRepository:
    """Own one dedicated bare project repository, independent of working files.

    No global Git configuration, hooks, shell, checkout, or user index participates.
    Portable UUIDs live in the same tree as the exact Markdown bytes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if self._git("rev-parse", "--is-bare-repository").strip() != b"true":
            raise ValueError("Git publication requires a dedicated bare project repository")

    @classmethod
    def create(cls, path: Path, *, project_external_id: UUID | None = None) -> GitProjectRepository:
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
        manifest = GitProjectManifest(project_external_id=project_external_id or uuid4())
        empty_tree = repository._write_tree({GIT_PROJECT_MANIFEST_PATH: manifest.canonical_json()})
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

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        _require_commit_id(ancestor)
        _require_commit_id(descendant)
        result = self._run("merge-base", "--is-ancestor", ancestor, descendant)
        if result.returncode not in {0, 1}:
            raise GitPublicationError("Cannot classify Git ancestry")
        return result.returncode == 0

    def relationship(
        self, local: str, remote: str | None
    ) -> Literal["unobserved", "equal", "ahead", "behind", "diverged"]:
        if remote is None:
            return "unobserved"
        if local == remote:
            return "equal"
        if self.is_ancestor(remote, local):
            return "ahead"
        if self.is_ancestor(local, remote):
            return "behind"
        return "diverged"

    def _transfer(self, *arguments: str) -> bytes:
        environment = self._environment()
        environment.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_SSH_COMMAND="ssh -oBatchMode=yes -oConnectTimeout=10",
        )
        result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "--git-dir",
                str(self.path),
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.file.allow=always",
                "-c",
                "protocol.ssh.allow=always",
                "-c",
                "protocol.https.allow=always",
                *arguments,
            ],
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )
        if result.returncode != 0:
            # Git stderr can include peer URLs and credential-helper output.
            raise GitPublicationError(
                "Git peer transfer failed; inspect protected host logs/config"
            )
        return result.stdout

    def fetch_published(self, remote: str, attempt_id: UUID) -> str | None:
        """Retain only the peer's published branch in an isolated incoming ref."""
        advertised = self._transfer("ls-remote", "--refs", "--", remote, PUBLISHED_REF)
        if not advertised.strip():
            return None
        reference = f"refs/bm/incoming/{attempt_id}"
        self._transfer(
            "fetch",
            "--no-tags",
            "--no-prune",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            "--no-auto-maintenance",
            "--refmap=",
            "--",
            remote,
            f"{PUBLISHED_REF}:{reference}",
        )
        return self._git("rev-parse", "--verify", reference).strip().decode("ascii")

    def incoming_snapshots(self, attempt_id: UUID, commit_id: str) -> list[GitSnapshot]:
        """Validate every newly received historical tree before accepting its tip."""
        _require_commit_id(commit_id)
        reference = f"refs/bm/incoming/{attempt_id}"
        if self._git("rev-parse", "--verify", reference).strip().decode("ascii") != commit_id:
            raise GitPublicationError("Incoming reference changed before validation")
        commits = self._git("rev-list", commit_id, "--not", PUBLISHED_REF).decode().splitlines()
        return [self._read_snapshot(commit) for commit in (commits or [commit_id])]

    def publish_incoming(self, attempt_id: UUID, commit_id: str, base_commit: str) -> None:
        """The same single-ref publication boundary, without inventing a local commit."""
        _require_commit_id(commit_id)
        _require_commit_id(base_commit)
        if not self.is_ancestor(base_commit, commit_id):
            raise GitPublicationError("Incoming publication must fast-forward local history")
        commands = (
            "start\n"
            f"verify refs/bm/incoming/{attempt_id} {commit_id}\n"
            f"update {PUBLISHED_REF} {commit_id} {base_commit}\n"
            "prepare\ncommit\n"
        )
        result = self._run("update-ref", "--stdin", input_bytes=commands.encode())
        if result.returncode != 0:
            actual = self.head()
            if actual != base_commit:
                raise GitPublicationConflict(base_commit, actual)
            raise GitPublicationError("Incoming reference changed before publication")

    def push_published(self, remote: str, commit_id: str) -> None:
        _require_commit_id(commit_id)
        if not self.is_ancestor(commit_id, self.head()):
            raise GitPublicationError("Only published ancestry can be sent to a Git peer")
        self._transfer(
            "push",
            "--no-verify",
            "--no-follow-tags",
            "--porcelain",
            "--",
            remote,
            f"{commit_id}:{PUBLISHED_REF}",
        )

    def initial_snapshot(self) -> GitSnapshot:
        """Require the empty initialization commit before adopting a legacy project."""
        snapshot = self.snapshot()
        parents = self._git("rev-list", "--parents", "-n", "1", snapshot.commit_id).split()
        if snapshot.files or len(parents) != 1:
            raise GitPublicationError("First adoption requires an unused project repository")
        return snapshot

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
        return self._read_snapshot(commit_id)

    def candidate_snapshot(self, prepared: PreparedGitPublication) -> GitSnapshot:
        """Read a retained private draft without allowing it into published readers."""
        _require_commit_id(prepared.base_commit)
        _require_commit_id(prepared.commit_id)
        expected_ref = f"refs/bm/candidates/{prepared.transaction_id}/{prepared.commit_id}"
        if (
            prepared.candidate_ref != expected_ref
            or self._git("rev-parse", "--verify", expected_ref).strip().decode("ascii")
            != prepared.commit_id
        ):
            raise GitPublicationError("Private draft reference does not match its candidate")
        return self._read_snapshot(prepared.commit_id)

    def remove_candidates(self, transaction_id: UUID) -> None:
        """Drop private retention only; published history and terminal receipts survive."""
        refs = self._git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            f"refs/bm/candidates/{transaction_id}/",
        ).decode("ascii")
        for line in refs.splitlines():
            reference, commit_id = line.split()
            self._git("update-ref", "-d", reference, commit_id)

    def _read_snapshot(self, commit_id: str) -> GitSnapshot:
        tree = self._git("ls-tree", "-r", "-z", "--full-tree", commit_id)
        files: dict[str, str] = {}
        manifest: GitProjectManifest | None = None
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, blob_id = metadata.split(b" ")
            if mode != b"100644" or kind != b"blob":
                raise GitPublicationError("Published project contains a non-regular note")
            path = raw_path.decode("utf-8")
            content = self._git("cat-file", "blob", blob_id.decode("ascii")).decode("utf-8")
            if path == GIT_PROJECT_MANIFEST_PATH:
                manifest = GitProjectManifest.model_validate_json(content)
                continue
            validate_note_path(path)
            files[path] = content
        if manifest is None or set(manifest.note_ids) != set(files):
            raise GitPublicationError(
                "Git manifest must identify every published note exactly once"
            )
        return GitSnapshot(commit_id, files, manifest)

    def prepare(
        self,
        *,
        transaction_id: UUID,
        base_commit: str,
        files: Mapping[str, str],
        note_ids: Mapping[str, UUID],
        message: str = "Publish Basic Memory notes",
    ) -> PreparedGitPublication:
        """Prepare a complete candidate without publishing bytes or holding a lock."""
        _require_commit_id(base_commit)
        if not message.strip() or "Basic-Memory-Transaction:" in message or "\0" in message:
            raise ValueError(
                "Commit message must be nonempty and omit reserved transaction evidence"
            )
        base = self.snapshot(base_commit)
        if set(note_ids) != set(files):
            raise ValueError("Every Markdown file requires exactly one explicit note identity")
        manifest = GitProjectManifest(
            project_external_id=base.manifest.project_external_id,
            notes=tuple(GitNoteIdentity(path=path, external_id=note_ids[path]) for path in files),
        )
        for path, external_id in base.manifest.note_ids.items():
            if path in note_ids and note_ids[path] != external_id:
                # An explicit permutation of existing UUIDs is a move/path swap.
                # Replacing a disappearing UUID or inventing a new owner at an
                # occupied path still requires a separate delete publication.
                if (
                    external_id not in note_ids.values()
                    or note_ids[path] not in base.manifest.note_ids.values()
                ):
                    raise ValueError(
                        "An existing note path cannot silently acquire a different identity"
                    )
        tree = self._write_tree({**files, GIT_PROJECT_MANIFEST_PATH: manifest.canonical_json()})
        base_tree = self._git("rev-parse", f"{base_commit}^{{tree}}").strip().decode("ascii")
        changed = tree != base_tree
        commit_id = (
            self._git(
                "commit-tree",
                tree,
                "-p",
                base_commit,
                input_bytes=(
                    f"{message.strip()}\n\nBasic-Memory-Transaction: {transaction_id}\n"
                ).encode(),
            )
            .strip()
            .decode("ascii")
            if changed
            else base_commit
        )
        candidate_ref = f"refs/bm/candidates/{transaction_id}/{commit_id}"
        self._git("update-ref", candidate_ref, commit_id)
        return PreparedGitPublication(
            transaction_id, base_commit, commit_id, candidate_ref, changed
        )

    def committed_at(self, commit_id: str) -> datetime:
        _require_commit_id(commit_id)
        timestamp = self._git("show", "--no-patch", "--format=%ct", commit_id).strip()
        return datetime.fromtimestamp(int(timestamp), tz=UTC)

    def contains_published_note_content(self, commit_id: str, path: str, content: str) -> bool:
        """Recognize an older published file without trusting private object existence."""
        _require_commit_id(commit_id)
        validate_note_path(path)
        self._git("merge-base", "--is-ancestor", commit_id, PUBLISHED_REF)
        blob = self._git("hash-object", "--stdin", input_bytes=content.encode("utf-8")).strip()
        objects = self._git(
            "rev-list", "--full-history", "--objects", "--no-object-names", commit_id, "--", path
        )
        return blob in objects.splitlines()

    def contains_published_note_identity(
        self, commit_id: str, external_id: UUID, path: str, content: str
    ) -> bool:
        """Prove UUID, old path and bytes belonged together in published ancestry."""
        _require_commit_id(commit_id)
        validate_note_path(path)
        self._git("merge-base", "--is-ancestor", commit_id, PUBLISHED_REF)
        # --full-history: a merge that is TREESAME to one parent would otherwise hide every
        # commit on the other parent, and a host whose own edit lives there could not prove it.
        commits = (
            self._git(
                "rev-list", "--full-history", commit_id, "--", path, GIT_PROJECT_MANIFEST_PATH
            )
            .decode("ascii")
            .splitlines()
        )
        for ancestor in commits:
            manifest = GitProjectManifest.model_validate_json(
                self._git("show", f"{ancestor}:{GIT_PROJECT_MANIFEST_PATH}")
            )
            if manifest.note_ids.get(path) == external_id and self._git(
                "show", f"{ancestor}:{path}"
            ) == content.encode("utf-8"):
                return True
        return False

    def _write_tree(self, files: Mapping[str, str]) -> str:
        """Persist an already validated tree using a private, temporary index."""
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
        return tree

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
