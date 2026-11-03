"""Prepare exact published Git content for BM's existing guarded materializer.

This opt-in experiment owns no background task and is not wired into startup/API.
Each SQL write is a short per-note transaction; a retry never creates a new UUID.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basic_memory import db
from basic_memory.file_utils import ParseError, compute_checksum, parse_frontmatter
from basic_memory.markdown.entity_parser import EntityParser
from basic_memory.models import Entity
from basic_memory.read_cache import ReadCacheInvalidator, invalidate_cache
from basic_memory.repository.entity_repository import AcceptedPendingEntityWrite
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_layout_repository import GitLayoutRepository, GitPreparedNote
from basic_memory.repository.git_projection_repository import (
    GitProjectionConflict,
    GitProjectionContentBasis,
    GitProjectionRepository,
)


@dataclass(frozen=True, slots=True)
class GitContentProjection:
    commit_id: str
    notes_total: int
    notes_changed: int
    notes_moved: int = 0
    notes_removed: int = 0


async def project_git_content(
    repository: GitProjectRepository,
    *,
    project_id: int,
    session_maker: async_sessionmaker[AsyncSession],
    read_cache: ReadCacheInvalidator | None = None,
    commit_id: str | None = None,
    redrive_materialization: bool = True,
) -> GitContentProjection:
    """Restore portable UUIDs and exact NoteContent, leaving file/index work pending.

    Call the existing materialization recovery and project index after this returns.
    The result confirms SQL content preparation, not complete file/graph/vector progress.
    """
    snapshot = await asyncio.to_thread(repository.snapshot, commit_id)
    if snapshot.commit_id != await asyncio.to_thread(repository.head):
        raise GitProjectionConflict("Projection requires the current published Git head")
    projection = GitProjectionRepository(project_id)
    layout = GitLayoutRepository(project_id)
    async with session_maker() as session:
        project = await projection.validate_project(session, snapshot.manifest)
        root = Path(project.path).resolve()
        local_notes = await layout.local_notes(session)
        foreign = await session.scalar(
            select(Entity.id)
            .where(
                Entity.external_id.in_([str(note.external_id) for note in snapshot.manifest.notes]),
                Entity.project_id != project_id,
            )
            .limit(1)
        )
        if foreign is not None:
            raise GitProjectionConflict("Note UUID is already owned by another project or path")
    if repository.path.is_relative_to(root) or root.is_relative_to(repository.path):
        raise GitProjectionConflict(
            "Internal Git repository must be outside the indexed project root"
        )
    observed_at = await asyncio.to_thread(repository.committed_at, snapshot.commit_id)
    parser = EntityParser(root)
    desired_paths = {note.external_id: note.path for note in snapshot.manifest.notes}
    local_by_uuid = {note.external_id: note for note in local_notes}
    for local in local_notes:
        if desired_paths.get(local.external_id) != local.file_path:
            if (
                local.content is None
                or local.content.checksum != await compute_checksum(local.content.markdown_content)
                or not await asyncio.to_thread(
                    repository.contains_published_note_identity,
                    snapshot.commit_id,
                    local.external_id,
                    local.file_path,
                    local.content.markdown_content,
                )
            ):
                raise GitProjectionConflict(
                    "Local notes outside this snapshot or a note path is already owned "
                    "without proven published ancestry"
                )
    prepared: dict[UUID, GitPreparedNote] = {}
    bases: dict[UUID, GitProjectionContentBasis | None] = {}
    for note in snapshot.manifest.notes:
        content = snapshot.files[note.path]
        local = local_by_uuid.get(note.external_id)
        basis = local.content if local is not None else None
        bases[note.external_id] = basis
        if (
            basis is not None
            and basis.markdown_content != content
            and not await asyncio.to_thread(
                repository.contains_published_note_identity,
                snapshot.commit_id,
                note.external_id,
                local.file_path if local is not None else note.path,
                basis.markdown_content,
            )
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
        prepared[note.external_id] = GitPreparedNote(
            fields, content, await compute_checksum(content)
        )
    changed = 0
    invalidation = (
        invalidate_cache(read_cache, str(snapshot.manifest.project_external_id))
        if read_cache is not None
        else nullcontext()
    )
    async with invalidation:
        if snapshot.commit_id != await asyncio.to_thread(repository.head):
            raise GitProjectionConflict(
                "Published Git head advanced during projection; retry latest"
            )
        async with db.scoped_session(session_maker) as session:
            moved, removed = await layout.apply(session, expected=local_notes, target=prepared)
        changed += moved
        for local in local_notes:
            if (
                target := prepared.get(local.external_id)
            ) is not None and local.file_path != target.fields.file_path:
                assert local.content is not None
                bases[local.external_id] = GitProjectionContentBasis(
                    local.entity_id,
                    local.content.db_version + 1,
                    target.checksum,
                    target.content,
                )
        for note in snapshot.manifest.notes:
            if snapshot.commit_id != await asyncio.to_thread(repository.head):
                raise GitProjectionConflict(
                    "Published Git head advanced during projection; retry latest"
                )
            async with db.scoped_session(session_maker) as session:
                target = prepared[note.external_id]
                restored = await projection.restore_note(
                    session,
                    external_id=note.external_id,
                    fields=target.fields,
                    markdown_content=target.content,
                    checksum=target.checksum,
                    expected=bases[note.external_id],
                    redrive_materialization=redrive_materialization,
                )
            changed += int(restored)
    return GitContentProjection(
        snapshot.commit_id, len(snapshot.manifest.notes), changed, moved, removed
    )
