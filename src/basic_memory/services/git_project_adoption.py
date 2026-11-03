"""Reviewable, restartable first adoption of a quiesced Markdown-only project.

The caller must stop and drain *all* legacy writers before capture/publication,
including API acceptance, materializers, editors and Cloud import. Projection
ownership excludes cooperating Git workers, not those legacy writers. No normal
runtime enables this isolated migration primitive yet.
"""

import asyncio
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basic_memory.file_utils import (
    ParseError,
    compute_checksum,
    parse_frontmatter,
    write_file_atomic_bytes,
)
from basic_memory.index.git_project_ownership import (
    GitLocalBinding,
    own_git_project,
    require_registered_git_root,
)
from basic_memory.markdown import EntityParser
from basic_memory.models import Entity, NoteContent, Project
from basic_memory.repository.git_layout_repository import GitLayoutRepository
from basic_memory.repository.git_project_repository import (
    GitProjectRepository,
    GitPublication,
    settle_git_publication,
)
from basic_memory.repository.git_projection_repository import GitProjectionConflict
from basic_memory.schemas.git_project import validate_note_path


class GitAdoptionAcceptedContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    markdown_content: str
    db_version: int
    db_checksum: str
    file_version: int | None
    file_checksum: str | None
    file_write_status: str


class GitAdoptionNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: int
    external_id: UUID
    path: str
    indexed_checksum: str | None
    accepted: GitAdoptionAcceptedContent | None
    # Preserve old materialized bytes as migration evidence, including CRLF.
    disk_content: str | None

    @property
    def content(self) -> str:
        if self.accepted is not None:
            return self.accepted.markdown_content
        assert self.disk_content is not None
        return self.disk_content


class GitAdoptionPlan(BaseModel):
    """Local operational evidence; retain with backups, never publish in note Git."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    binding: GitLocalBinding
    transaction_id: UUID
    base_commit: str
    notes: tuple[GitAdoptionNote, ...]


def raise_inventory_error(error: OSError) -> None:
    raise error


def read_adoption_files(root: Path) -> dict[str, str]:
    """Inventory exact UTF-8 regular Markdown files, refusing unknown resources."""
    files: dict[str, str] = {}
    for parent, directories, names in root.walk(
        follow_symlinks=False, on_error=raise_inventory_error
    ):
        for directory in tuple(directories):
            path = parent / directory
            if path.is_symlink():
                raise GitProjectionConflict("Adoption refuses symlink directories")
            if path == root / ".basic-memory":
                if any(
                    child.name not in {"git-binding.json", "git-projection.lock"}
                    for child in path.iterdir()
                ):
                    raise GitProjectionConflict("Adoption found unknown project control files")
                directories.remove(directory)
        for name in names:
            path = parent / name
            if path.is_symlink() or not path.is_file():
                raise GitProjectionConflict("Adoption requires regular Markdown files")
            relative = path.relative_to(root).as_posix()
            validate_note_path(relative)
            files[relative] = path.read_bytes().decode("utf-8")
    return files


async def capture_adoption_notes(
    *, project_id: int, root: Path, session_maker: async_sessionmaker[AsyncSession]
) -> tuple[GitAdoptionNote, ...]:
    files = await asyncio.to_thread(read_adoption_files, root)
    async with session_maker() as session:
        # This also verifies mirrored UUID/project/path ownership in NoteContent.
        await GitLayoutRepository(project_id).local_notes(session)
        rows = await session.execute(
            select(Entity, NoteContent)
            .outerjoin(NoteContent, Entity.id == NoteContent.entity_id)
            .where(Entity.project_id == project_id)
            .order_by(Entity.id)
        )
        notes = tuple(
            GitAdoptionNote(
                entity_id=entity.id,
                external_id=UUID(entity.external_id),
                path=entity.file_path,
                indexed_checksum=entity.checksum,
                accepted=(
                    GitAdoptionAcceptedContent(
                        markdown_content=content.markdown_content,
                        db_version=content.db_version,
                        db_checksum=content.db_checksum,
                        file_version=content.file_version,
                        file_checksum=content.file_checksum,
                        file_write_status=content.file_write_status,
                    )
                    if content is not None
                    else None
                ),
                disk_content=files.get(entity.file_path),
            )
            for entity, content in rows
            if entity.content_type == "text/markdown"
        )
        count = len(
            (await session.scalars(select(Entity.id).where(Entity.project_id == project_id))).all()
        )
    if len(notes) != count:
        raise GitProjectionConflict("Adoption does not support non-Markdown entities")
    if set(files) - {note.path for note in notes}:
        raise GitProjectionConflict("Adoption found unindexed files; import them explicitly first")
    parser = EntityParser(root)
    for note in notes:
        validate_note_path(note.path)
        disk_checksum = (
            await compute_checksum(note.disk_content) if note.disk_content is not None else None
        )
        if note.accepted is not None:
            accepted = note.accepted
            if accepted.db_checksum != await compute_checksum(accepted.markdown_content):
                raise GitProjectionConflict("Accepted content checksum is inconsistent")
            if disk_checksum is not None and disk_checksum not in {
                accepted.db_checksum,
                accepted.file_checksum,
            }:
                raise GitProjectionConflict("Adoption found an unpublished editor change")
        elif disk_checksum is None or disk_checksum != note.indexed_checksum:
            raise GitProjectionConflict("File-first adoption requires the indexed file checksum")
        # Validate everything before creating any Git candidate or changing SQL.
        try:
            metadata = parse_frontmatter(note.content)
        except ParseError as error:
            raise GitProjectionConflict("Adoption requires canonical BM frontmatter") from error
        if any(
            not isinstance(metadata.get(key), str) or not metadata[key]
            for key in ("title", "type", "permalink")
        ):
            raise GitProjectionConflict("Adoption requires canonical title/type/permalink")
        await parser.parse_markdown_content(file_path=Path(note.path), content=note.content)
    return notes


async def prepare_git_adoption(
    repository: GitProjectRepository,
    *,
    project: Project,
    session_maker: async_sessionmaker[AsyncSession],
) -> GitAdoptionPlan:
    """Capture an immutable plan while legacy writers are externally quiesced.

    Repeated preparation returns the retained plan. It never silently refreshes
    a changed basis; publication must revalidate it or the operator must review
    and archive the old evidence before starting another migration.
    """
    root = Path(project.path).resolve()
    async with own_git_project(
        repository,
        project_id=project.id,
        project_external_id=project.external_id,
        project_root=root,
        session_maker=session_maker,
    ):
        path = repository.path / "bm-adoption.json"
        if path.is_symlink():
            raise GitProjectionConflict("Adoption evidence must not be a symlink")
        binding = GitLocalBinding(
            project_external_id=UUID(project.external_id),
            project_root=str(root),
            repository_path=str(repository.path),
        )
        if path.exists():
            plan = GitAdoptionPlan.model_validate_json(path.read_bytes())
            if plan.binding != binding:
                raise GitProjectionConflict("Adoption evidence belongs to another binding")
            return plan
        snapshot = await asyncio.to_thread(repository.initial_snapshot)
        notes = await capture_adoption_notes(
            project_id=project.id, root=root, session_maker=session_maker
        )
        plan = GitAdoptionPlan(
            binding=binding,
            transaction_id=uuid4(),
            base_commit=snapshot.commit_id,
            notes=notes,
        )
        async with session_maker() as session:
            await require_registered_git_root(
                session, project_id=project.id, project_external_id=project.external_id, root=root
            )
        await write_file_atomic_bytes(path, (plan.model_dump_json(indent=2) + "\n").encode())
        return plan


async def publish_git_adoption(
    repository: GitProjectRepository,
    *,
    project: Project,
    session_maker: async_sessionmaker[AsyncSession],
) -> GitPublication:
    """Publish retained accepted bytes once, with fixed-basis/source revalidation.

    A published transaction is resolved before comparing the old SQL/disk basis:
    projection may already have advanced it after a lost response. No SQL or note
    file is changed here; the ordinary Git projector performs subsequent catch-up.
    """
    root = Path(project.path).resolve()
    async with own_git_project(
        repository,
        project_id=project.id,
        project_external_id=project.external_id,
        project_root=root,
        session_maker=session_maker,
    ):
        path = repository.path / "bm-adoption.json"
        if path.is_symlink():
            raise GitProjectionConflict("Adoption evidence must not be a symlink")
        plan = GitAdoptionPlan.model_validate_json(path.read_bytes())
        if plan.binding != GitLocalBinding(
            project_external_id=UUID(project.external_id),
            project_root=str(root),
            repository_path=str(repository.path),
        ):
            raise GitProjectionConflict("Adoption evidence belongs to another binding")
        previous = await asyncio.to_thread(repository.transaction_status, plan.transaction_id)
        if previous is not None:
            return previous
        if (await asyncio.to_thread(repository.initial_snapshot)).commit_id != plan.base_commit:
            raise GitProjectionConflict("Adoption Git basis changed")
        prepared = await asyncio.to_thread(
            repository.prepare,
            transaction_id=plan.transaction_id,
            base_commit=plan.base_commit,
            files={note.path: note.content for note in plan.notes},
            note_ids={note.path: note.external_id for note in plan.notes},
        )
        current = await capture_adoption_notes(
            project_id=project.id, root=root, session_maker=session_maker
        )
        if current != plan.notes:
            raise GitProjectionConflict("Adoption SQL/file basis changed; review retained plan")
        async with session_maker() as session:
            await require_registered_git_root(
                session, project_id=project.id, project_external_id=project.external_id, root=root
            )
        return await settle_git_publication(repository, prepared)
