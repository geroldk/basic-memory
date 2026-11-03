"""Prepare exact published Git content for BM's existing guarded materializer.

This opt-in experiment owns no background task and is not wired into startup/API.
Each SQL write is a short per-note transaction; a retry never creates a new UUID.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basic_memory.file_utils import ParseError, compute_checksum, parse_frontmatter
from basic_memory.markdown.entity_parser import EntityParser
from basic_memory.read_cache import ReadCacheInvalidator, invalidate_cache
from basic_memory.repository.entity_repository import AcceptedPendingEntityWrite
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_projection_repository import (
    GitProjectionConflict,
    GitProjectionRepository,
)


@dataclass(frozen=True, slots=True)
class GitContentProjection:
    commit_id: str
    notes_total: int
    notes_changed: int


async def project_git_content(
    repository: GitProjectRepository,
    *,
    project_id: int,
    session_maker: async_sessionmaker[AsyncSession],
    read_cache: ReadCacheInvalidator | None = None,
    commit_id: str | None = None,
) -> GitContentProjection:
    """Restore portable UUIDs and exact NoteContent, leaving file/index work pending.

    Call the existing materialization recovery and project index after this returns.
    The result confirms SQL content preparation, not complete file/graph/vector progress.
    """
    snapshot = await asyncio.to_thread(repository.snapshot, commit_id)
    if snapshot.commit_id != await asyncio.to_thread(repository.head):
        raise GitProjectionConflict("Projection requires the current published Git head")
    projection = GitProjectionRepository(project_id)
    async with session_maker() as session:
        project = await projection.validate_project(session, snapshot.manifest)
        root = Path(project.path).resolve()
        bases = {
            note.path: await projection.read_basis(session, note.external_id, note.path)
            for note in snapshot.manifest.notes
        }
    if repository.path.is_relative_to(root) or root.is_relative_to(repository.path):
        raise GitProjectionConflict(
            "Internal Git repository must be outside the indexed project root"
        )
    observed_at = await asyncio.to_thread(repository.committed_at, snapshot.commit_id)
    parser = EntityParser(root)
    prepared: list[tuple[AcceptedPendingEntityWrite, str, str]] = []
    for note in snapshot.manifest.notes:
        content = snapshot.files[note.path]
        basis = bases[note.path]
        if basis is not None and not await asyncio.to_thread(
            repository.contains_published_note_content,
            snapshot.commit_id,
            note.path,
            basis.markdown_content,
        ):
            raise GitProjectionConflict(
                "Local SQL contains bytes outside the target's published history"
            )
        markdown = await parser.parse_markdown_content(
            file_path=Path(note.path),
            content=content,
            mtime=observed_at.timestamp(),
            ctime=observed_at.timestamp(),
        )
        try:
            metadata = parse_frontmatter(content)
        except ParseError as error:
            raise GitProjectionConflict("Projection requires canonical BM frontmatter") from error
        if (
            markdown.frontmatter_state != "present"
            or not isinstance(metadata.get("title"), str)
            or not metadata["title"]
            or not isinstance(metadata.get("permalink"), str)
            or not metadata["permalink"]
            or not isinstance(metadata.get("type"), str)
            or not metadata["type"]
        ):
            raise GitProjectionConflict(
                "Projection requires canonical BM title/type/permalink frontmatter"
            )
        fields = AcceptedPendingEntityWrite(
            title=markdown.frontmatter.title,
            note_type=markdown.frontmatter.type,
            entity_metadata=markdown.frontmatter.metadata,
            content_type="text/markdown",
            permalink=markdown.frontmatter.permalink,
            file_path=note.path,
            created_at=markdown.created or observed_at,
            updated_at=markdown.modified or observed_at,
            created_by=None,
            last_updated_by=None,
            external_id=str(note.external_id),
        )
        prepared.append((fields, content, await compute_checksum(content)))
    changed = 0
    invalidation = (
        invalidate_cache(read_cache, str(snapshot.manifest.project_external_id))
        if read_cache is not None
        else nullcontext()
    )
    async with invalidation:
        for note, (fields, content, checksum) in zip(
            snapshot.manifest.notes, prepared, strict=True
        ):
            if snapshot.commit_id != await asyncio.to_thread(repository.head):
                raise GitProjectionConflict(
                    "Published Git head advanced during projection; retry latest"
                )
            async with session_maker() as session, session.begin():
                restored = await projection.restore_note(
                    session,
                    external_id=note.external_id,
                    fields=fields,
                    markdown_content=content,
                    checksum=checksum,
                    expected=bases[note.path],
                )
            changed += int(restored)
    return GitContentProjection(snapshot.commit_id, len(snapshot.manifest.notes), changed)
