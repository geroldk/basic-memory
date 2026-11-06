"""Automatic note-level merge of diverged histories against real local Git peers."""

import json
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from basic_memory.api.container import resolve_container
from basic_memory.config_git import GitSyncRemote
from basic_memory.repository.git_project_repository import GitProjectRepository

from tests.git_host_support import host_setup as host_setup, ready


def git(*args):
    return subprocess.run(
        ["git", *map(str, args)], capture_output=True, check=True, timeout=30
    ).stdout


def note(title, body="- [fact] Probe\n"):
    return f"---\ntitle: {title}\ntype: note\npermalink: {title.lower()}\n---\n{body}"


def publish(repo, files, ids=None):
    """Publish the full tree `files`; `ids` maps path to UUID (existing paths keep theirs)."""
    base = repo.snapshot()
    ids = {
        path: (ids or {}).get(path) or base.manifest.note_ids.get(path) or uuid4() for path in files
    }
    prepared = repo.prepare(
        transaction_id=uuid4(), base_commit=base.commit_id, files=files, note_ids=ids
    )
    return repo.publish(prepared).commit_id


def add(repo, **notes):
    """Publish the current tree plus `name=body` notes (file `<name>.md`)."""
    files = {**repo.snapshot().files, **{f"{n}.md": note(n, b) for n, b in notes.items()}}
    return publish(repo, files)


@pytest.fixture
def merge_peer(host_setup, tmp_path, config_manager):
    path = tmp_path / "peer.git"
    git("clone", "--bare", "--no-local", host_setup.repository.path, path)
    peer = GitProjectRepository(path)
    project = UUID(host_setup.project.external_id)
    host_setup.config.git_sync_remotes = {project: GitSyncRemote(url=str(path))}
    host_setup.config.git_sync_merge = {project: "auto"}
    config_manager.save_config(host_setup.config)
    return peer


def sync_service(project_id):
    host = resolve_container().git_host
    assert host is not None
    return host.runtime_for(project_id).sync


async def converge(host_setup):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        await ready(client, project)


@pytest.mark.asyncio
async def test_independent_additions_merge_push_and_converge(host_setup, merge_peer):
    from basic_memory.api.app import app

    local_tip = add(host_setup.repository, mine="Mine\n")
    remote_tip = add(merge_peer, theirs="Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        result = await service.synchronize()
        assert result.last_attempt.outcome == "merged"
        head = host_setup.repository.head()
        assert head == merge_peer.head() != local_tip
        for tip in (local_tip, remote_tip):
            assert host_setup.repository.is_ancestor(tip, head)
        assert set(host_setup.repository.snapshot().files) == {"mine.md", "theirs.md"}
        assert result.relationship == "equal"
        await converge(host_setup)
        root = host_setup.project.path
        assert sorted(p.name for p in Path(root).glob("*.md")) == ["mine.md", "theirs.md"]
        # The next run has nothing left to do.
        assert (await service.synchronize()).last_attempt.outcome == "equal"


async def _two_sided(host_setup, merge_peer, *, host_moves: bool):
    """Case 5: one side moves a note, the other edits its bytes; both directions."""
    from basic_memory.api.app import app

    add(host_setup.repository, a="Original\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        assert (await service.synchronize()).last_attempt.outcome == "pushed"
        await converge(host_setup)
    uuid = host_setup.repository.snapshot().manifest.note_ids["a.md"]
    mover, editor = (
        (host_setup.repository, merge_peer) if host_moves else (merge_peer, host_setup.repository)
    )
    publish(mover, {"moved/a.md": note("a", "Original\n")}, {"moved/a.md": uuid})
    publish(editor, {"a.md": note("a", "Edited\n")})
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        assert (await service.synchronize()).last_attempt.outcome == "merged"
        merged = host_setup.repository.snapshot()
        assert merged.files == {"moved/a.md": note("a", "Edited\n")}
        assert merged.manifest.note_ids == {"moved/a.md": uuid}
        await converge(host_setup)
        root = Path(host_setup.project.path)
        assert (root / "moved/a.md").read_text() == note("a", "Edited\n")
        assert not (root / "a.md").exists()


@pytest.mark.asyncio
async def test_move_here_edit_there_converges_through_the_worker(host_setup, merge_peer):
    await _two_sided(host_setup, merge_peer, host_moves=True)


@pytest.mark.asyncio
async def test_edit_here_move_there_converges_through_the_worker(host_setup, merge_peer):
    """The host's own bytes are reachable only via the merge's second parent (P1)."""
    await _two_sided(host_setup, merge_peer, host_moves=False)


@pytest.mark.asyncio
async def test_conflict_writes_a_review_package_and_publishes_nothing(host_setup, merge_peer):
    from basic_memory.api.app import app

    add(host_setup.repository, shared="Base\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        await service.synchronize()
    local_tip = publish(host_setup.repository, {"shared.md": note("shared", "Mine\n")})
    remote_tip = publish(merge_peer, {"shared.md": note("shared", "Theirs\n")})
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        result = await service.synchronize()
        assert result.last_attempt.outcome == "needs_review"
        assert host_setup.repository.head() == local_tip and merge_peer.head() == remote_tip
        assert result.open_review is not None
        assert result.open_review.conflicts == {"both_changed": 1}
        assert result.open_review.paths == ["shared.md"]
        directory = host_setup.repository.path / "bm-merge-review" / result.open_review.id
        report = json.loads((directory / "konflikte.json").read_text())
        assert report["local"] == local_tip and report["remote"] == remote_tip
        assert report["conflicts"][0]["kind"] == "both_changed"
        uuid = report["conflicts"][0]["notes"][0]["external_id"]
        parts = {p.name for p in (directory / "001" / uuid).iterdir()}
        assert parts == {"basis.md", "lokal.md", "fern.md", "vorschlag.md"}
        assert "<<<<<<< lokal" in (directory / "001" / uuid / "vorschlag.md").read_text()
        # Waiting is stable: the same pair gives the same package and the same answer.
        again = await service.synchronize()
        assert again.last_attempt.outcome == "needs_review"
        assert again.open_review == result.open_review


@pytest.mark.asyncio
async def test_a_peer_that_advances_during_the_push_is_merged_again_next_run(
    host_setup, merge_peer, monkeypatch
):
    from basic_memory.api.app import app

    add(host_setup.repository, mine="Mine\n")
    add(merge_peer, theirs="Theirs\n")
    original = GitProjectRepository.push_published
    advanced = []

    def racing(self, remote, commit):
        if not advanced:
            advanced.append(add(merge_peer, late="Late\n"))
        return original(self, remote, commit)

    monkeypatch.setattr(GitProjectRepository, "push_published", racing)
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        first = await service.synchronize()
        # The merge is already published locally, the push was refused: nothing is lost, and
        # the observed remote must not jump to the merge (that would look like a rewrite).
        assert first.last_attempt.kind == "failed" and first.last_attempt.reason == "transport"
        assert first.observed_remote_commit != host_setup.repository.head()
        second = await service.synchronize()
        assert second.last_attempt.outcome == "merged"
        assert host_setup.repository.head() == merge_peer.head()
        assert set(host_setup.repository.snapshot().files) == {
            "mine.md",
            "theirs.md",
            "late.md",
        }
        assert (await service.synchronize()).last_attempt.outcome == "equal"


@pytest.mark.asyncio
async def test_crash_between_local_publication_and_push_is_finished_by_the_next_run(
    host_setup, merge_peer, monkeypatch
):
    from basic_memory.api.app import app

    add(host_setup.repository, mine="Mine\n")
    add(merge_peer, theirs="Theirs\n")
    original = GitProjectRepository.push_published
    calls = []

    def dying(self, remote, commit):
        if not calls:
            calls.append(commit)
            raise OSError("process died")
        return original(self, remote, commit)

    monkeypatch.setattr(GitProjectRepository, "push_published", dying)
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        assert (await service.synchronize()).last_attempt.kind == "failed"
        merged = host_setup.repository.head()
        assert merged == calls[0] and merge_peer.head() != merged
        again = await service.synchronize()
        assert again.last_attempt.outcome == "pushed" and merge_peer.head() == merged


@pytest.mark.asyncio
async def test_local_tip_moving_during_the_computation_fails_as_local_changed(
    host_setup, merge_peer, monkeypatch
):
    from basic_memory.api.app import app

    add(host_setup.repository, mine="Mine\n")
    add(merge_peer, theirs="Theirs\n")
    original = GitProjectRepository.prepare_merge
    moved = []

    def interfering(self, **kwargs):
        prepared = original(self, **kwargs)
        moved.append(add(host_setup.repository, during="During\n"))
        return prepared

    monkeypatch.setattr(GitProjectRepository, "prepare_merge", interfering)
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        result = await service.synchronize()
        assert result.last_attempt.kind == "failed"
        assert result.last_attempt.reason == "local_changed"
        assert host_setup.repository.head() == moved[0]
        monkeypatch.setattr(GitProjectRepository, "prepare_merge", original)
        assert (await service.synchronize()).last_attempt.outcome == "merged"
        assert "during.md" in host_setup.repository.snapshot().files


@pytest.mark.asyncio
async def test_repeated_divergence_keeps_a_valid_history(host_setup, merge_peer):
    from basic_memory.api.app import app

    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        for round_ in range(3):
            add(host_setup.repository, **{f"mine{round_}": "M\n"})
            add(merge_peer, **{f"theirs{round_}": "T\n"})
            assert (await service.synchronize()).last_attempt.outcome == "merged"
            assert host_setup.repository.head() == merge_peer.head()
        assert len(host_setup.repository.snapshot().files) == 6
        git("--git-dir", merge_peer.path, "fsck", "--strict")
        await converge(host_setup)


@pytest.mark.asyncio
async def test_criss_cross_history_is_reported_not_merged(host_setup, merge_peer):
    from basic_memory.api.app import app

    base_one = add(host_setup.repository, base="Base\n")
    git(
        "--git-dir",
        merge_peer.path,
        "fetch",
        "--no-tags",
        host_setup.repository.path,
        "refs/heads/main:refs/heads/main",
        "--force",
    )
    x = add(host_setup.repository, x="X\n")
    y = add(merge_peer, y="Y\n")
    git(
        "--git-dir",
        host_setup.repository.path,
        "fetch",
        "--no-tags",
        merge_peer.path,
        f"{y}:refs/review/y",
    )
    git(
        "--git-dir",
        merge_peer.path,
        "fetch",
        "--no-tags",
        host_setup.repository.path,
        f"{x}:refs/review/x",
    )
    both = {**host_setup.repository.snapshot(x).files, **merge_peer.snapshot(y).files}
    ids = {
        **host_setup.repository.snapshot(x).manifest.note_ids,
        **merge_peer.snapshot(y).manifest.note_ids,
    }
    from basic_memory.schemas.git_project import (
        GIT_PROJECT_MANIFEST_PATH,
        GitNoteIdentity,
        GitProjectManifest,
    )

    manifest = GitProjectManifest(
        project_external_id=host_setup.repository.snapshot().manifest.project_external_id,
        notes=tuple(GitNoteIdentity(path=p, external_id=i) for p, i in ids.items()),
    )

    def cross(repo, parents):
        tree = repo._write_tree({**both, GIT_PROJECT_MANIFEST_PATH: manifest.canonical_json()})
        args = [a for p in parents for a in ("-p", p)]
        return repo._git("commit-tree", tree, *args, input_bytes=b"cross\n").strip().decode()

    m1, m2 = cross(host_setup.repository, [x, y]), cross(merge_peer, [y, x])
    host_setup.repository._git("update-ref", "refs/heads/main", m1)
    merge_peer._git("update-ref", "refs/heads/main", m2)
    publish(host_setup.repository, {**both, "h.md": note("h")})
    publish(merge_peer, {**both, "p.md": note("p")})
    assert base_one  # both sides share it
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        result = await service.synchronize()
        assert result.last_attempt.outcome == "diverged"
        assert (
            len(
                host_setup.repository.merge_bases(
                    result.local_commit, result.observed_remote_commit
                )
            )
            == 2
        )


@pytest.mark.asyncio
async def test_merge_off_keeps_reporting_divergence(host_setup, merge_peer, config_manager):
    from basic_memory.api.app import app

    host_setup.config.git_sync_merge = {}
    config_manager.save_config(host_setup.config)
    add(host_setup.repository, mine="Mine\n")
    add(merge_peer, theirs="Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        assert (await service.synchronize()).last_attempt.outcome == "diverged"


@pytest.mark.asyncio
async def test_a_rewritten_remote_is_never_merged(host_setup, merge_peer):
    from basic_memory.api.app import app

    add(host_setup.repository, mine="Mine\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        assert (await service.synchronize()).last_attempt.outcome == "pushed"
    add(merge_peer, fresh="Fresh\n")
    first = merge_peer._git("rev-list", "--max-parents=0", "main").decode().split()[0]
    merge_peer._git("update-ref", "refs/heads/main", first)
    add(host_setup.repository, more="More\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        assert (await service.synchronize()).last_attempt.outcome == "remote_rewritten"


@pytest.mark.asyncio
async def test_merge_settings_must_refer_to_projects_with_a_peer(host_setup):
    from basic_memory import db
    from basic_memory.index.git_host import run_git_host
    from basic_memory.repository.git_projection_repository import GitProjectionConflict

    config = host_setup.config.model_copy(deep=True)
    config.git_sync_merge = {uuid4(): "auto"}
    _, factory = await db.get_or_create_db(config.database_path)
    with pytest.raises(GitProjectionConflict, match="merge settings"):
        async with run_git_host(config, factory):
            pytest.fail("Merge setting without a peer admitted")


def _decide(host_setup, result, decisions):
    """Fill in the package the way a person would; returns the package directory."""
    directory = host_setup.repository.path / "bm-merge-review" / result.open_review.id
    document = json.loads((directory / "konflikte.json").read_text())
    for conflict, decision in zip(document["conflicts"], decisions, strict=True):
        conflict["decision"] = decision
    (directory / "konflikte.json").write_text(json.dumps(document))
    return directory


async def _conflicted(host_setup, merge_peer, local, remote, base=None):
    """Diverge on `shared.md` with the given bodies and return the first needs_review status."""
    from basic_memory.api.app import app

    add(host_setup.repository, shared=base or "Base\n")
    async with app.router.lifespan_context(app):
        await sync_service(host_setup.project.external_id).synchronize()
    local_tip = publish(host_setup.repository, {"shared.md": note("shared", local)})
    remote_tip = publish(merge_peer, {"shared.md": note("shared", remote)})
    return local_tip, remote_tip


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("lokal", "Mine\n"), ("fern", "Theirs\n")])
async def test_a_decision_picks_one_side_and_publishes_a_reviewed_merge(
    host_setup, merge_peer, action, expected
):
    from basic_memory.api.app import app

    local_tip, remote_tip = await _conflicted(host_setup, merge_peer, "Mine\n", "Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        status = await service.synchronize()
        _decide(host_setup, status, [{"action": action}])
        result = await service.synchronize(review=status.open_review.id)
        assert result.last_attempt.outcome == "merged"
        head = host_setup.repository.head()
        assert head == merge_peer.head()
        assert host_setup.repository.snapshot().files == {"shared.md": note("shared", expected)}
        message = host_setup.repository._git("show", "--no-patch", "--format=%B", head).decode()
        assert "Basic-Memory-Review:" in message
        assert host_setup.repository.is_ancestor(local_tip, head)
        assert host_setup.repository.is_ancestor(remote_tip, head)
        await converge(host_setup)


@pytest.mark.asyncio
async def test_both_keeps_the_remote_version_as_a_marked_second_note(host_setup, merge_peer):
    from basic_memory.api.app import app

    await _conflicted(host_setup, merge_peer, "Mine\n", "Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        status = await service.synchronize()
        _decide(host_setup, status, [{"action": "beide"}])
        assert (await service.synchronize(review=status.open_review.id)).last_attempt.outcome == (
            "merged"
        )
        files = host_setup.repository.snapshot().files
        assert set(files) == {"shared.md", "shared-fern.md"}
        assert files["shared.md"] == note("shared", "Mine\n")
        assert (
            "Theirs" in files["shared-fern.md"]
            and "permalink: shared-fern" in files["shared-fern.md"]
        )
        await converge(host_setup)


@pytest.mark.asyncio
async def test_an_edited_text_is_taken_from_the_package(host_setup, merge_peer):
    from basic_memory.api.app import app

    await _conflicted(host_setup, merge_peer, "Mine\n", "Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        status = await service.synchronize()
        directory = _decide(host_setup, status, [{"action": "bearbeitet"}])
        uuid = next((directory / "001").iterdir())
        with pytest.raises(ValueError, match="bearbeitet.md is missing"):
            await service.synchronize(review=status.open_review.id)
        (uuid / "bearbeitet.md").write_text(note("shared", "Both\n"))
        result = await service.synchronize(review=status.open_review.id)
        assert result.last_attempt.outcome == "merged"
        assert host_setup.repository.snapshot().files == {"shared.md": note("shared", "Both\n")}


@pytest.mark.asyncio
async def test_undecided_or_foreign_review_requests_are_refused_without_an_attempt(
    host_setup, merge_peer
):
    from basic_memory.api.app import app

    local_tip, _ = await _conflicted(host_setup, merge_peer, "Mine\n", "Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        status = await service.synchronize()
        before = status.last_attempt
        with pytest.raises(ValueError, match="Undecided"):
            await service.synchronize(review=status.open_review.id)
        with pytest.raises(ValueError, match="Not a review package id"):
            await service.synchronize(review="../../etc")
        with pytest.raises(ValueError, match="missing or unreadable"):
            await service.synchronize(review="0" * 12 + "-" + "0" * 12)
        assert (await service.status()).last_attempt == before
        assert host_setup.repository.head() == local_tip


@pytest.mark.asyncio
async def test_a_decision_made_for_old_versions_is_carried_not_applied(host_setup, merge_peer):
    from basic_memory.api.app import app

    await _conflicted(host_setup, merge_peer, "Mine\n", "Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        status = await service.synchronize()
        old = status.open_review.id
        _decide(host_setup, status, [{"action": "lokal"}])
        # Another unrelated note arrives on the peer after the decision was written.
        add(merge_peer, extra="Extra\n")
        moved = await service.synchronize(review=old)
        assert moved.last_attempt.outcome == "needs_review"
        assert moved.open_review.id != old
        package = host_setup.repository.path / "bm-merge-review" / moved.open_review.id
        carried = json.loads((package / "konflikte.json").read_text())["conflicts"][0]["decision"]
        assert carried == {"action": "lokal"}  # versions unchanged, so the decision survives
        done = await service.synchronize(review=moved.open_review.id)
        assert done.last_attempt.outcome == "merged"
        assert set(host_setup.repository.snapshot().files) == {"shared.md", "extra.md"}


@pytest.mark.asyncio
async def test_repeated_sync_runs_keep_the_decisions_already_entered(host_setup, merge_peer):
    from basic_memory.api.app import app

    await _conflicted(host_setup, merge_peer, "Mine\n", "Theirs\n")
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        status = await service.synchronize()
        directory = _decide(host_setup, status, [{"action": "fern"}])
        (next((directory / "001").iterdir()) / "bearbeitet.md").write_text("keep me")
        again = await service.synchronize()
        assert again.last_attempt.outcome == "needs_review"
        document = json.loads((directory / "konflikte.json").read_text())
        assert document["conflicts"][0]["decision"] == {"action": "fern"}
        assert (next((directory / "001").iterdir()) / "bearbeitet.md").read_text() == "keep me"


@pytest.mark.asyncio
async def test_merge_application_requires_the_switch(host_setup, merge_peer, config_manager):
    from basic_memory.api.app import app

    host_setup.config.git_sync_merge = {}
    config_manager.save_config(host_setup.config)
    async with app.router.lifespan_context(app):
        service = sync_service(host_setup.project.external_id)
        with pytest.raises(ValueError, match="not enabled"):
            await service.synchronize(review="0" * 12 + "-" + "0" * 12)
