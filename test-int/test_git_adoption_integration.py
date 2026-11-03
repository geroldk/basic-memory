"""First adoption preserves accepted bytes and refuses unreviewed local changes."""

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update

from basic_memory.file_utils import compute_checksum
from basic_memory.index.git_projection import build_git_projection_pipeline
from basic_memory.index.git_projection_worker import GitProjectionWorker
from basic_memory.index.note_content_materialization import LocalNoteContentMaterializationProvider
from basic_memory.models import Entity, NoteContent, Project
from basic_memory.repository.git_project_repository import GitProjectRepository, GitPublicationError
from basic_memory.repository.git_projection_repository import GitProjectionConflict
from basic_memory.services.git_project_adoption import (
    prepare_git_adoption,
    publish_git_adoption,
    read_adoption_files,
)


@pytest.fixture(autouse=True)
async def separate_note_root(test_project, engine_factory, app_config):
    # The shared integration fixture puts SQLite in the original project root.
    # Migration must refuse unknown resources, so keep runtime state outside it.
    root = Path(test_project.path) / "project-notes"
    root.mkdir()
    test_project.path = str(root)
    app_config.projects[test_project.name].path = str(root)
    async with engine_factory[1]() as session, session.begin():
        await session.execute(
            update(Project).where(Project.id == test_project.id).values(path=str(root))
        )


@pytest.fixture
def repository(tmp_path_factory, test_project):
    return GitProjectRepository.create(
        tmp_path_factory.mktemp("git-adoption") / "project.git",
        project_external_id=UUID(test_project.external_id),
    )


async def write_note(client, test_project, text="Initialprobe"):
    response = await client.post(
        f"/v2/projects/{test_project.external_id}/knowledge/write",
        json={"note": {"title": "Legacy", "directory": "notes", "content": f"- [fact] {text}\n"}},
    )
    assert response.status_code == 200, response.text
    return response.json()["entity"]


async def wait_ready(worker):
    async with asyncio.timeout(15):
        while not (await worker.status()).caught_up:
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("materialized", [False, True])
async def test_adopt_real_accepted_unwritten_content_and_index_after_publication(
    repository, test_project, engine_factory, app_config, client, monkeypatch, materialized
):
    if materialized:
        original = await write_note(client, test_project, "Oldfileprobe")
    else:
        original = None

    async def leave_pending(self, accepted):
        return accepted

    # The real API accepts into NoteContent; suspend only the file follow-up.
    monkeypatch.setattr(
        LocalNoteContentMaterializationProvider, "materialize_write_change", leave_pending
    )
    pending = (
        await write_note(client, test_project, "Acceptedpendingprobe") if original is None else None
    )
    if original is not None:
        response = await client.patch(
            f"/v2/projects/{test_project.external_id}/knowledge/entities/{original['external_id']}",
            json={"operation": "append", "content": "\n- [fact] Acceptedpendingprobe\n"},
        )
        assert response.status_code == 202, response.text
        pending = original
    assert pending is not None
    session_maker = engine_factory[1]
    async with session_maker() as session:
        content = await session.get(NoteContent, pending["id"])
        assert content is not None
        accepted_bytes = content.markdown_content
        accepted_version = content.db_version
    plan = await prepare_git_adoption(repository, project=test_project, session_maker=session_maker)
    assert len(plan.notes) == 1
    assert plan.notes[0].external_id == UUID(pending["external_id"])
    assert plan.notes[0].content == accepted_bytes
    assert (plan.notes[0].disk_content is not None) == materialized
    if materialized:
        assert plan.notes[0].disk_content != accepted_bytes
    assert repository.snapshot().files == {}
    publication = await publish_git_adoption(
        repository, project=test_project, session_maker=session_maker
    )
    assert repository.snapshot().files[pending["file_path"]] == accepted_bytes
    # Publication itself changes neither SQL nor the protected old/missing file.
    path = Path(test_project.path) / pending["file_path"]
    assert (path.read_bytes().decode() if path.exists() else None) == plan.notes[0].disk_content
    pipeline = build_git_projection_pipeline(
        repository, project=test_project, session_maker=session_maker, app_config=app_config
    )
    worker = GitProjectionWorker(pipeline, poll_interval=0.02)
    async with worker.run():
        await wait_ready(worker)
    assert path.read_bytes() == accepted_bytes.encode()
    async with session_maker() as session:
        current = await session.get(NoteContent, pending["id"])
        assert current is not None and current.db_version == accepted_version
        assert current.external_id == pending["external_id"]
        assert current.file_write_status == "synced"
    # A lost answer is resolved before rechecking the now-advanced materialization.
    again = await publish_git_adoption(
        repository, project=test_project, session_maker=session_maker
    )
    assert again.already_committed and again.commit_id == publication.commit_id
    assert (
        await prepare_git_adoption(repository, project=test_project, session_maker=session_maker)
        == plan
    )
    found = await client.request(
        "QUERY",
        f"/v2/projects/{test_project.external_id}/search/",
        json={"text": "Acceptedpendingprobe", "retrieval_mode": "fts"},
    )
    assert found.status_code == 200 and "Acceptedpendingprobe" in found.text
    chunks = await pipeline.search_service.repository.get_entity_chunk_manifest(pending["id"])
    assert chunks and all(row.embedding_status == "ready" for row in chunks)


@pytest.mark.asyncio
async def test_file_first_adoption_preserves_crlf_and_identity(
    repository, test_project, engine_factory, client
):
    note = await write_note(client, test_project)
    path = Path(test_project.path) / note["file_path"]
    content = path.read_bytes().decode().replace("\n", "\r\n")
    path.write_bytes(content.encode())
    async with engine_factory[1]() as session, session.begin():
        await session.execute(delete(NoteContent).where(NoteContent.entity_id == note["id"]))
        await session.execute(
            update(Entity)
            .where(Entity.id == note["id"])
            .values(checksum=await compute_checksum(content))
        )
    plan = await prepare_git_adoption(
        repository, project=test_project, session_maker=engine_factory[1]
    )
    assert plan.notes[0].accepted is None
    await publish_git_adoption(repository, project=test_project, session_maker=engine_factory[1])
    assert repository.snapshot().files[note["file_path"]] == content
    assert repository.snapshot().manifest.note_ids[note["file_path"]] == UUID(note["external_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["sql", "file", "new_file", "deleted_entity"])
async def test_changed_sources_refuse_publication_and_retain_plan(
    repository, test_project, engine_factory, client, change
):
    note = await write_note(client, test_project)
    session_maker = engine_factory[1]
    plan = await prepare_git_adoption(repository, project=test_project, session_maker=session_maker)
    evidence = (repository.path / "bm-adoption.json").read_bytes()
    path = Path(test_project.path) / note["file_path"]
    if change == "sql":
        async with session_maker() as session, session.begin():
            await session.execute(
                update(NoteContent)
                .where(NoteContent.entity_id == note["id"])
                .values(db_version=999)
            )
    elif change == "file":
        path.write_text("Private editor draft")
    elif change == "new_file":
        (Path(test_project.path) / "unindexed.md").write_text("Unindexed note")
    else:
        async with session_maker() as session, session.begin():
            entity = await session.get(Entity, note["id"])
            await session.delete(entity)
    with pytest.raises(GitProjectionConflict):
        await publish_git_adoption(repository, project=test_project, session_maker=session_maker)
    assert repository.head() == plan.base_commit
    assert (repository.path / "bm-adoption.json").read_bytes() == evidence
    assert path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "checksum",
        "missing_file_first",
        "changed_file_first",
        "non_markdown",
        "missing_frontmatter",
        "missing_permalink",
        "malformed_frontmatter",
        "foreign_mirror",
    ],
)
async def test_adoption_refuses_unproven_or_noncanonical_sources(
    repository, test_project, engine_factory, client, invalid
):
    note = await write_note(client, test_project)
    path = Path(test_project.path) / note["file_path"]
    async with engine_factory[1]() as session, session.begin():
        if invalid == "checksum":
            await session.execute(
                update(NoteContent)
                .where(NoteContent.entity_id == note["id"])
                .values(db_checksum="wrong")
            )
        elif invalid in {"missing_file_first", "changed_file_first"}:
            await session.execute(delete(NoteContent).where(NoteContent.entity_id == note["id"]))
            if invalid == "missing_file_first":
                path.unlink()
            else:
                path.write_text("Private draft")
        elif invalid == "non_markdown":
            await session.execute(
                update(Entity).where(Entity.id == note["id"]).values(content_type="image/png")
            )
        elif invalid == "foreign_mirror":
            await session.execute(
                update(NoteContent)
                .where(NoteContent.entity_id == note["id"])
                .values(file_path="elsewhere.md")
            )
        else:
            content = {
                "missing_frontmatter": "No frontmatter",
                "missing_permalink": "---\ntitle: Legacy\ntype: note\n---\nBody",
                "malformed_frontmatter": "---\ntitle: [invalid\n---\nBody",
            }[invalid]
            path.write_bytes(content.encode())
            checksum = await compute_checksum(content)
            await session.execute(
                update(NoteContent)
                .where(NoteContent.entity_id == note["id"])
                .values(markdown_content=content, db_checksum=checksum, file_checksum=checksum)
            )
    with pytest.raises((GitProjectionConflict, ValueError)):
        await prepare_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )
    assert repository.snapshot().files == {}
    assert not (repository.path / "bm-adoption.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["resource", "directory_symlink", "file_symlink"])
async def test_unknown_resources_and_symlinks_are_not_silently_dropped(
    repository, test_project, engine_factory, tmp_path, extra
):
    root = Path(test_project.path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("Protected")
    if extra == "resource":
        (root / "picture.png").write_bytes(b"image")
    elif extra == "directory_symlink":
        (root / "linked").symlink_to(outside, target_is_directory=True)
    else:
        (root / "linked.md").symlink_to(outside / "note.md")
    with pytest.raises((GitProjectionConflict, ValueError)):
        await prepare_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )
    assert (outside / "note.md").read_text() == "Protected"


@pytest.mark.asyncio
async def test_empty_adoption_is_a_repeatable_noop(repository, test_project, engine_factory):
    original = repository.head()
    plan = await prepare_git_adoption(
        repository, project=test_project, session_maker=engine_factory[1]
    )
    assert not plan.notes
    result = await publish_git_adoption(
        repository, project=test_project, session_maker=engine_factory[1]
    )
    assert result.commit_id == original
    assert (
        await publish_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )
    ).already_committed


@pytest.mark.asyncio
async def test_existing_published_history_cannot_be_reinitialized(
    repository, test_project, engine_factory
):
    base = repository.head()
    repository.publish(
        repository.prepare(
            transaction_id=uuid4(),
            base_commit=base,
            files={"a.md": "Other"},
            note_ids={"a.md": uuid4()},
        )
    )
    repository.publish(
        repository.prepare(
            transaction_id=uuid4(), base_commit=repository.head(), files={}, note_ids={}
        )
    )
    with pytest.raises(GitPublicationError, match="unused"):
        await prepare_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before", "after"])
async def test_real_process_exit_around_adoption_publication(
    repository, test_project, engine_factory, client, app_config, tmp_path, fault
):
    note = await write_note(client, test_project)
    plan = await prepare_git_adoption(
        repository, project=test_project, session_maker=engine_factory[1]
    )

    def run_child() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable,
                "test-int/fixtures/git_adoption_crash.py",
                str(engine_factory[0].url.database),
                str(repository.path),
                str(test_project.id),
                fault,
            ],
            env={**os.environ, "PYTHONPATH": "src:tests"},
            capture_output=True,
            timeout=30,
        )

    child = await asyncio.to_thread(run_child)
    assert child.returncode == 78, child.stderr.decode()
    old_status = repository.transaction_status(plan.transaction_id)
    assert (old_status is not None) == (fault == "after")
    result = await publish_git_adoption(
        repository, project=test_project, session_maker=engine_factory[1]
    )
    assert result.already_committed == (fault == "after")
    assert repository.snapshot().manifest.note_ids[note["file_path"]] == UUID(note["external_id"])
    assert (
        await publish_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )
    ).commit_id == result.commit_id


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "publish"])
@pytest.mark.parametrize("fault", ["symlink", "binding"])
async def test_adoption_evidence_requires_its_original_binding_and_regular_file(
    repository, test_project, engine_factory, tmp_path, phase, fault
):
    import json

    await prepare_git_adoption(repository, project=test_project, session_maker=engine_factory[1])
    path = repository.path / "bm-adoption.json"
    if fault == "symlink":
        protected = tmp_path / "protected-evidence.json"
        path.rename(protected)
        path.symlink_to(protected)
    else:
        data = json.loads(path.read_bytes())
        data["binding"]["project_root"] = "/different/local/root"
        path.write_text(json.dumps(data))
    operation = prepare_git_adoption if phase == "prepare" else publish_git_adoption
    with pytest.raises(GitProjectionConflict):
        await operation(repository, project=test_project, session_maker=engine_factory[1])
    assert repository.snapshot().files == {}


@pytest.mark.asyncio
async def test_unknown_control_files_are_not_omitted(repository, test_project, engine_factory):
    control = Path(test_project.path) / ".basic-memory"
    control.mkdir()
    protected = control / "unexpected.md"
    protected.write_text("Private control note")
    with pytest.raises(GitProjectionConflict, match="unknown project control"):
        await prepare_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )
    assert protected.read_text() == "Private control note"


@pytest.mark.asyncio
async def test_unreadable_directory_is_not_silently_omitted(
    repository, test_project, engine_factory
):
    protected = Path(test_project.path) / "unreadable"
    protected.mkdir()
    (protected / "private.md").write_text("Private")
    protected.chmod(0)
    try:
        with pytest.raises(PermissionError):
            await prepare_git_adoption(
                repository, project=test_project, session_maker=engine_factory[1]
            )
    finally:
        protected.chmod(0o700)
    assert not (repository.path / "bm-adoption.json").exists()


@pytest.mark.asyncio
async def test_replaced_initial_git_basis_requires_review(repository, test_project, engine_factory):
    from basic_memory.repository.git_project_repository import PUBLISHED_REF

    plan = await prepare_git_adoption(
        repository, project=test_project, session_maker=engine_factory[1]
    )
    tree = repository._git("rev-parse", f"{plan.base_commit}^{{tree}}").strip().decode()
    changed = (
        repository._git("commit-tree", tree, input_bytes=b"Different initialization\n")
        .strip()
        .decode()
    )
    repository._git("update-ref", PUBLISHED_REF, changed, plan.base_commit)
    with pytest.raises(GitProjectionConflict, match="Git basis changed"):
        await publish_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )
    assert repository.head() == changed


@pytest.mark.asyncio
async def test_cancelled_adoption_keeps_ownership_until_publication_settles(
    repository, test_project, engine_factory, client, monkeypatch
):
    await write_note(client, test_project)
    await prepare_git_adoption(repository, project=test_project, session_maker=engine_factory[1])
    entered, release = threading.Event(), threading.Event()
    publish = GitProjectRepository.publish

    def paused_publish(self, prepared):
        entered.set()
        assert release.wait(10)
        return publish(self, prepared)

    monkeypatch.setattr(GitProjectRepository, "publish", paused_publish)
    task = asyncio.create_task(
        publish_git_adoption(repository, project=test_project, session_maker=engine_factory[1])
    )
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        task.cancel()
        await asyncio.sleep(0.02)
        with pytest.raises(GitProjectionConflict, match="already running"):
            await prepare_git_adoption(
                repository, project=test_project, session_maker=engine_factory[1]
            )
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (
        await publish_git_adoption(
            repository, project=test_project, session_maker=engine_factory[1]
        )
    ).already_committed


@pytest.mark.asyncio
async def test_directory_replaced_with_symlink_during_inventory_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    directory = root / "changed"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "private.md"
    protected.write_text("Private")
    walk = Path.walk

    def replace_after_inventory(self, **kwargs):
        for parent, directories, names in walk(self, **kwargs):
            if parent == root:
                directory.rmdir()
                directory.symlink_to(outside, target_is_directory=True)
            yield parent, directories, names

    monkeypatch.setattr(Path, "walk", replace_after_inventory)
    with pytest.raises(GitProjectionConflict, match="symlink directories"):
        read_adoption_files(root)
    assert protected.read_text() == "Private"
