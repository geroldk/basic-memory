"""Review package for a merge that needs a person's decision.

Written next to the bare repository (never inside an indexed note folder). Holds the three
versions of each conflicting note and, for a note changed on both sides, a line-level
proposal that is only a reading aid and is never applied automatically. Contents of notes
are written here and nowhere else; reports and status carry only paths and counts.
"""

from __future__ import annotations

import hashlib
import shutil
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from basic_memory.repository.git_project_repository import GitSnapshot
from basic_memory.schemas.git_sync import GitSyncReview
from basic_memory.services.git_merge_resolution import (
    EDITED_FILE,
    conflict_key,
    load_document,
)
from basic_memory.services.git_merge import MergePlan, NoteState, note_states

REVIEW_DIRECTORY = "bm-merge-review"
REVIEW_FORMAT = 1


def review_id(local: str, remote: str) -> str:
    return f"{local[:12]}-{remote[:12]}"


def _digest(state: NoteState | None) -> str | None:
    return hashlib.sha256(state.content.encode()).hexdigest() if state else None


def _proposal(base: str, local: str, remote: str) -> str:
    """Line-level three-way text with conflict marks (git merge-file), for reading only."""
    with TemporaryDirectory(prefix="bm-merge-") as directory:
        files = []
        for name, text in (("lokal", local), ("basis", base), ("fern", remote)):
            path = Path(directory) / name
            path.write_text(text, encoding="utf-8")
            files.append(str(path))
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                "-L",
                "lokal",
                "-L",
                "basis",
                "-L",
                "fern",
                *files,
            ],
            capture_output=True,
            check=False,
            timeout=30,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        )
    if result.returncode < 0 or result.returncode > 127:
        raise OSError("git merge-file failed")
    return result.stdout.decode("utf-8", errors="replace")


def write_review_package(
    root: Path,
    *,
    project_id: UUID,
    base: GitSnapshot,
    local: GitSnapshot,
    remote: GitSnapshot,
    plan: MergePlan,
    previous: Path | None = None,
) -> Path:
    """Write (or rewrite, idempotently) the package for exactly this pair of tips.

    `previous` is an older package of the same project: decisions (and finished texts) for
    conflicts whose versions are unchanged are carried over, all others start undecided.
    """
    carried: dict[str, tuple[dict[str, Any], Path]] = {}
    directory = root / review_id(local.commit_id, remote.commit_id)
    # A repeated sync run for the same pair of tips must never wipe decisions already entered.
    if previous is None and (directory / "konflikte.json").exists():
        previous = directory
    if previous is not None:
        for old in load_document(previous)["conflicts"]:
            if old.get("decision"):
                carried[conflict_key(old)] = (old["decision"], previous / f"{old['number']:03d}")
    states = {"base": note_states(base), "local": note_states(local), "remote": note_states(remote)}
    directory.mkdir(parents=True, exist_ok=True)
    conflicts = []
    for number, conflict in enumerate(plan.conflicts, start=1):
        entry_dir = directory / f"{number:03d}"
        for external_id in conflict.external_ids:
            note_dir = entry_dir / str(external_id)
            note_dir.mkdir(parents=True, exist_ok=True)
            for side, name in (("base", "basis"), ("local", "lokal"), ("remote", "fern")):
                if (state := states[side].get(external_id)) is not None:
                    (note_dir / f"{name}.md").write_text(state.content, encoding="utf-8")
            if conflict.kind == "both_changed":
                texts = [
                    state.content if (state := states[side].get(external_id)) else ""
                    for side in ("base", "local", "remote")
                ]
                (note_dir / "vorschlag.md").write_text(_proposal(*texts), encoding="utf-8")
        entry = {
            "number": number,
            "kind": conflict.kind,
            "notes": [
                {
                    "external_id": str(external_id),
                    "paths": {
                        side: state.path
                        for side in ("base", "local", "remote")
                        if (state := states[side].get(external_id)) is not None
                    },
                    "sha256": {
                        side: _digest(states[side].get(external_id))
                        for side in ("base", "local", "remote")
                    },
                }
                for external_id in conflict.external_ids
            ],
            "decision": None,
        }
        if (kept := carried.get(conflict_key(entry))) is not None:
            entry["decision"] = kept[0]
            for external_id in conflict.external_ids:
                source = kept[1] / str(external_id) / EDITED_FILE
                destination = entry_dir / str(external_id) / EDITED_FILE
                if source.is_file() and source != destination:
                    shutil.copyfile(source, destination)
        conflicts.append(entry)
    document = {
        "format_version": REVIEW_FORMAT,
        "project_id": str(project_id),
        "base": base.commit_id,
        "local": local.commit_id,
        "remote": remote.commit_id,
        "conflicts": conflicts,
    }
    target = directory / "konflikte.json"
    temporary = directory / ".konflikte.json.tmp"
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return directory


def read_review(root: Path, identifier: str) -> GitSyncReview | None:
    """What status may show: conflict counts per kind and note paths, never contents."""
    try:
        document = json.loads((root / identifier / "konflikte.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    counts: dict[str, int] = {}
    paths: list[str] = []
    for conflict in document["conflicts"]:
        counts[conflict["kind"]] = counts.get(conflict["kind"], 0) + 1
        paths += sorted({p for note in conflict["notes"] for p in note["paths"].values()})
    return GitSyncReview(id=identifier, conflicts=counts, paths=sorted(set(paths)))
