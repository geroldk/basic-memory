"""Owned API/MCP hosts, real local Git peers, offline writes and derived convergence."""

import asyncio
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastmcp import Client
from httpx import ASGITransport, AsyncClient

from basic_memory.api.container import resolve_container
from basic_memory.config_git import GitSyncRemote
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_sync_repository import GitSyncRepository
from basic_memory.schemas.git_sync import SyncRunning

from test_git_host_integration import host_setup as host_setup, ready


def git(*args):
    return subprocess.run(
        ["git", *map(str, args)], capture_output=True, check=True, timeout=30
    ).stdout


def note(title, body="- [fact] Peerprobe\n"):
    return f"---\ntitle: {title}\ntype: note\npermalink: {title.lower()}\n---\n{body}"


def publish(peer, files):
    base = peer.snapshot()
    ids = {path: base.manifest.note_ids.get(path, uuid4()) for path in files}
    prepared = peer.prepare(
        transaction_id=uuid4(), base_commit=base.commit_id, files=files, note_ids=ids
    )
    return peer.publish(prepared).commit_id


@pytest.fixture
def sync_peer(host_setup, tmp_path, config_manager):
    path = tmp_path / "peer.git"
    git("clone", "--bare", "--no-local", host_setup.repository.path, path)
    peer = GitProjectRepository(path)
    host_setup.config.git_sync_remotes = {
        UUID(host_setup.project.external_id): GitSyncRemote(url=str(path))
    }
    config_manager.save_config(host_setup.config)
    return peer


def runtime(project_id):
    host = resolve_container().git_host
    assert host is not None
    return host.runtime_for(project_id)


@pytest.mark.asyncio
async def test_api_pull_indexes_and_keeps_old_private_transaction(host_setup, sync_peer):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    path = f"/v2/projects/{project}"
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            txpath = path + "/git/transactions"
            started = (await client.post(txpath, json={})).json()
            tx = txpath + "/" + started["transaction_id"]
            headers = {"X-BM-Transaction-Key": started["transaction_key"]}
            draft = await client.post(
                tx + "/write",
                headers=headers,
                json={
                    "expected_revision": 0,
                    "path": "private.md",
                    "content": note("Private", "Neverexported\n"),
                },
            )
            assert draft.status_code == 200, draft.text
            incoming = publish(sync_peer, {"incoming.md": note("Incoming")})
            answer = await client.post(path + "/git/sync")
            assert answer.status_code == 200, answer.text
            assert answer.headers["Cache-Control"] == "no-store"
            assert answer.json()["last_attempt"]["outcome"] == "pulled"
            assert answer.json()["local_commit"] == incoming
            status = await ready(client, project)
            assert status["synchronization"]["relationship"] == "equal"
            identity = str(sync_peer.snapshot().manifest.note_ids["incoming.md"])
            read = await client.get(path + "/knowledge/entities/" + identity)
            assert read.status_code == 200 and read.json()["content"] == note("Incoming")
            found = await client.request(
                "QUERY", path + "/search/", json={"text": "Peerprobe", "retrieval_mode": "fts"}
            )
            assert found.status_code == 200 and "Peerprobe" in found.text
            refused = await client.post(
                tx + "/commit", headers=headers, json={"expected_revision": 1}
            )
            assert refused.status_code == 409
            own = await client.get(
                tx + "/note", headers=headers, params={"identifier": "private.md"}
            )
            assert own.status_code == 200 and "Neverexported" in own.text
            assert "private.md" not in host_setup.repository.snapshot().files
            written = await client.post(
                path + "/knowledge/write",
                json={"note": {"title": "Local", "content": "Localprobe\n"}},
            )
            assert written.status_code == 200, written.text
            pushed = await client.post(path + "/git/sync")
            assert pushed.json()["last_attempt"]["outcome"] == "pushed"
            assert sync_peer.head() == host_setup.repository.head()
            assert "incoming.md" in sync_peer.snapshot().files
            assert "private.md" not in sync_peer.snapshot().files
            equal = await client.post(path + "/git/sync")
            assert equal.json()["last_attempt"]["outcome"] == "equal"
    async with app.router.lifespan_context(app):
        recovered = await runtime(project).sync.status()
        assert recovered.last_attempt is not None and recovered.last_attempt.kind == "completed"
        assert recovered.relationship == "equal"


@pytest.mark.asyncio
async def test_offline_writes_and_empty_remote_initialization(host_setup, sync_peer):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    peer_path = sync_peer.path
    moved = peer_path.with_name("offline.git")
    peer_path.rename(moved)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            path = f"/v2/projects/{project}"
            failed = await client.post(path + "/git/sync")
            assert (
                failed.status_code == 200 and failed.json()["last_attempt"]["reason"] == "transport"
            )
            assert str(peer_path) not in failed.text
            written = await client.post(
                path + "/knowledge/write",
                json={"note": {"title": "Offline", "content": "Offlineprobe\n"}},
            )
            assert written.status_code == 200
            await ready(client, project)
            git("init", "--bare", "--initial-branch=main", peer_path)
            answer = await client.post(path + "/git/sync")
            assert answer.json()["last_attempt"]["outcome"] == "pushed"
            assert GitProjectRepository(peer_path).head() == host_setup.repository.head()


@pytest.mark.asyncio
async def test_divergence_preserves_both_histories_and_rewrites_keep_stopping(
    host_setup, sync_peer
):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    initial = sync_peer.head()
    async with app.router.lifespan_context(app):
        service = runtime(project).sync
        await service.synchronize()
        peer_tip = publish(sync_peer, {"peer.md": note("Peer")})
        local_tip = publish(host_setup.repository, {"local.md": note("Local")})
        diverged = await service.synchronize()
        assert diverged.last_attempt.outcome == "diverged"
        assert (host_setup.repository.head(), sync_peer.head()) == (local_tip, peer_tip)
        git("--git-dir", sync_peer.path, "update-ref", "refs/heads/main", initial)
        for _ in range(2):
            stopped = await service.synchronize()
            assert stopped.last_attempt.outcome == "remote_rewritten"
            assert stopped.observed_remote_commit == peer_tip
        git("--git-dir", sync_peer.path, "update-ref", "-d", "refs/heads/main")
        deleted = await service.synchronize()
        assert deleted.last_attempt.outcome == "remote_rewritten"
        assert host_setup.repository.head() == local_tip
        assert (
            git(
                "--git-dir",
                host_setup.repository.path,
                "for-each-ref",
                "--format=%(objectname)",
                "refs/bm/incoming",
            )
            .splitlines()
            .count(peer_tip.encode())
            >= 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["frontmatter", "foreign_project", "historical", "symlink"])
async def test_reject_incoming_before_publication(host_setup, sync_peer, invalid):
    from basic_memory.api.app import app
    from basic_memory.schemas.git_project import GIT_PROJECT_MANIFEST_PATH, GitProjectManifest

    initial = host_setup.repository.head()
    if invalid == "foreign_project":
        tree = sync_peer._write_tree(
            {
                GIT_PROJECT_MANIFEST_PATH: GitProjectManifest(
                    project_external_id=uuid4()
                ).canonical_json()
            }
        )
    elif invalid == "symlink":
        blob = (
            sync_peer._git("hash-object", "-w", "--stdin", input_bytes=b"/tmp/secret")
            .strip()
            .decode()
        )
        tree = (
            sync_peer._git("mktree", input_bytes=f"120000 blob {blob}\tsecret.md\n".encode())
            .strip()
            .decode()
        )
    else:
        publish(sync_peer, {"bad.md": "No canonical frontmatter"})
        if invalid == "historical":
            publish(sync_peer, {})
        tree = None
    if tree is not None:
        commit = (
            sync_peer._git("commit-tree", tree, "-p", initial, input_bytes=b"Untrusted peer\n")
            .strip()
            .decode()
        )
        sync_peer._git("update-ref", "refs/heads/main", commit)
    async with app.router.lifespan_context(app):
        result = await runtime(host_setup.project.external_id).sync.synchronize()
        assert (
            result.last_attempt.kind == "failed"
            and result.last_attempt.reason == "invalid_incoming"
        )
        assert host_setup.repository.head() == initial
        assert host_setup.repository.snapshot().files == {}


@pytest.mark.asyncio
async def test_network_wait_does_not_lock_local_writes(host_setup, sync_peer, monkeypatch):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    entered, release = threading.Event(), threading.Event()
    original = GitProjectRepository.fetch_published

    def blocked(self, remote, attempt):
        entered.set()
        assert release.wait(20)
        return original(self, remote, attempt)

    monkeypatch.setattr(GitProjectRepository, "fetch_published", blocked)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            url = f"/v2/projects/{project}"
            task = asyncio.create_task(client.post(url + "/git/sync"))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                busy = await client.post(url + "/git/sync")
                assert busy.status_code == 409
                written = await asyncio.wait_for(
                    client.post(
                        url + "/knowledge/write",
                        json={
                            "note": {"title": "Whileoffline", "content": "Accepted without peer\n"}
                        },
                    ),
                    5,
                )
                assert written.status_code == 200
                status = await client.get(url + "/git/status")
                assert status.json()["synchronization"]["last_attempt"]["kind"] == "running"
            finally:
                release.set()
                result = await task
            assert result.json()["last_attempt"]["outcome"] == "pushed"


@pytest.mark.asyncio
async def test_repeated_cancellation_settles_transport_and_restart_marks_interruption(
    host_setup, sync_peer, monkeypatch
):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    entered, release = threading.Event(), threading.Event()
    original = GitProjectRepository.push_published

    def blocked(self, remote, commit):
        entered.set()
        assert release.wait(20)
        original(self, remote, commit)

    monkeypatch.setattr(GitProjectRepository, "push_published", blocked)
    async with app.router.lifespan_context(app):
        service = runtime(project).sync
        publish(host_setup.repository, {"local.md": note("Local")})
        task = asyncio.create_task(service.synchronize())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0.02)
            assert service.lock.locked() and not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert sync_peer.head() == host_setup.repository.head()
        assert (await service.status()).last_attempt.reason == "interrupted"
        record = service.records.load().model_copy(
            update={"attempt": SyncRunning(attempt_id=uuid4(), started_at=datetime.now(UTC))}
        )
        service.records.save(record)
    async with app.router.lifespan_context(app):
        service = runtime(project).sync
        assert (await service.status()).last_attempt.reason == "interrupted"
        assert (await service.synchronize()).last_attempt.outcome == "equal"


@pytest.mark.asyncio
async def test_api_status_failure_and_unconfigured_peer(host_setup, monkeypatch):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    async with app.router.lifespan_context(app):
        service = runtime(project).sync
        with pytest.raises(ValueError, match="No Git peer"):
            await service.synchronize()
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            result = await client.post(f"/v2/projects/{project}/git/sync")
            assert result.status_code == 409
            # A failed private status write must never appear as a completed sync.
            service.remote = GitSyncRemote(url="/tmp/unreachable-peer")

            def fail(self, record):
                raise OSError("contains credential material")

            monkeypatch.setattr(GitSyncRepository, "save", fail)
            result = await client.post(f"/v2/projects/{project}/git/sync")
            assert result.status_code == 503 and "credential" not in result.text


@pytest.mark.asyncio
async def test_compare_and_swap_conflict_during_pull_is_visible(host_setup, sync_peer, monkeypatch):
    from basic_memory.api.app import app

    publish(sync_peer, {"peer.md": note("Peer")})
    original = GitProjectRepository.publish_incoming

    def raced(self, attempt, incoming, base):
        actual = publish(self, {"race.md": note("Race")})
        assert actual != base
        original(self, attempt, incoming, base)

    monkeypatch.setattr(GitProjectRepository, "publish_incoming", raced)
    async with app.router.lifespan_context(app):
        answer = await runtime(host_setup.project.external_id).sync.synchronize()
        assert answer.last_attempt.reason == "local_changed"
        assert host_setup.repository.snapshot().files == {"race.md": note("Race")}


@pytest.mark.asyncio
async def test_mcp_sync_uses_normal_project_routing(host_setup, sync_peer):
    from basic_memory.mcp.server import mcp

    publish(sync_peer, {"mcp.md": note("Mcp")})
    async with Client(mcp) as client:
        result = await client.call_tool(
            "bm_project", {"command": "sync", "project": "test-project", "output_format": "json"}
        )
        assert result.data["last_attempt"]["outcome"] == "pulled"
        text = await client.call_tool(
            "bm_project", {"command": "sync", "project": "test-project", "output_format": "text"}
        )
        assert "equal" in str(text)
        status = await client.call_tool(
            "bm_project", {"command": "status", "project": "test-project", "output_format": "text"}
        )
        assert "last observation" in str(status)
        with pytest.raises(Exception, match="sync needs"):
            await client.call_tool("bm_project", {"command": "sync", "output_format": "json"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "fetch_before",
        "fetch_after",
        "publish_before",
        "publish_after",
        "push_before",
        "push_after",
        "status_after",
    ],
)
async def test_whole_api_death_and_lost_answers_recover_without_an_extra_commit(
    host_setup, sync_peer, tmp_path, phase
):
    from basic_memory.api.app import app

    project = host_setup.project.external_id
    initial = host_setup.repository.head()
    source = host_setup.repository if phase.startswith("push_") else sync_peer
    target = publish(source, {"recover.md": note("Recover", "- [fact] Recoveryprobe\n")})
    request = tmp_path / "sync-crash.json"
    request.write_text(json.dumps({"project_id": project, "phase": phase}))
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "test-int/fixtures/git_sync_crash.py", str(request)],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 83, child.stderr
    published = phase in {"publish_after", "status_after"} or phase.startswith("push_")
    assert host_setup.repository.head() == (target if published else initial)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            status = await ready(client, project)
            attempt = status["synchronization"]["last_attempt"]
            assert attempt["kind"] == ("completed" if phase == "status_after" else "failed")
            if phase != "status_after":
                assert attempt["reason"] == "interrupted"
            result = await client.post(f"/v2/projects/{project}/git/sync")
            assert result.status_code == 200, result.text
            assert result.json()["relationship"] == "equal"
            await ready(client, project)
            assert host_setup.repository.head() == sync_peer.head() == target
            assert host_setup.repository.snapshot().files == {
                "recover.md": note("Recover", "- [fact] Recoveryprobe\n")
            }
            assert (
                git("--git-dir", host_setup.repository.path, "rev-list", "--count", "main").strip()
                == b"2"
            )


@pytest.mark.asyncio
async def test_one_project_diverges_while_another_syncs_independently(
    host_setup, sync_peer, config_manager, tmp_path
):
    from basic_memory import db
    from basic_memory.api.app import app
    from basic_memory.config import ProjectEntry
    from basic_memory.repository.project_repository import ProjectRepository

    config = host_setup.config
    root = tmp_path / "second"
    root.mkdir()
    config.projects["second"] = ProjectEntry(path=str(root))
    _, factory = await db.get_or_create_db(config.database_path)
    async with db.scoped_session(factory) as session:
        second = await ProjectRepository().create(
            session, {"name": "second", "path": str(root), "is_active": True}
        )
    repository = GitProjectRepository.create(
        tmp_path / "second.git", project_external_id=UUID(second.external_id)
    )
    peer_path = tmp_path / "second-peer.git"
    git("clone", "--bare", "--no-local", repository.path, peer_path)
    peer = GitProjectRepository(peer_path)
    config.git_repositories[UUID(second.external_id)] = repository.path
    config.git_sync_remotes[UUID(second.external_id)] = GitSyncRemote(url=str(peer_path))
    config_manager.save_config(config)
    local_a = publish(host_setup.repository, {"a.md": note("A")})
    peer_a = publish(sync_peer, {"b.md": note("B")})
    peer_b = publish(peer, {"c.md": note("C")})
    async with app.router.lifespan_context(app):
        a, b = await asyncio.gather(
            runtime(host_setup.project.external_id).sync.synchronize(),
            runtime(second.external_id).sync.synchronize(),
        )
        assert a.last_attempt.outcome == "diverged"
        assert b.last_attempt.outcome == "pulled"
        assert host_setup.repository.head() == local_a and sync_peer.head() == peer_a
        assert repository.head() == peer_b


@pytest.mark.asyncio
async def test_unknown_peer_project_refuses_host_startup(host_setup):
    from basic_memory import db
    from basic_memory.index.git_host import run_git_host
    from basic_memory.repository.git_projection_repository import GitProjectionConflict

    config = host_setup.config.model_copy(deep=True)
    config.git_sync_remotes[uuid4()] = GitSyncRemote(url="/tmp/unknown-project")
    _, factory = await db.get_or_create_db(config.database_path)
    with pytest.raises(GitProjectionConflict, match="peers must refer"):
        async with run_git_host(config, factory):
            pytest.fail("Unknown project peer admitted")


@pytest.mark.asyncio
async def test_legacy_api_refuses_git_sync(client):
    result = await client.post("/v2/projects/unused/git/sync")
    assert result.status_code == 404


@pytest.mark.asyncio
async def test_explicitly_reviewed_two_parent_merge_converges_without_reset(host_setup, sync_peer):
    from basic_memory.api.app import app
    from basic_memory.schemas.git_project import (
        GIT_PROJECT_MANIFEST_PATH,
        GitNoteIdentity,
        GitProjectManifest,
    )

    local = host_setup.repository
    local_tip = publish(local, {"local.md": note("Local")})
    remote_tip = publish(sync_peer, {"peer.md": note("Peer")})
    async with app.router.lifespan_context(app):
        service = runtime(host_setup.project.external_id).sync
        assert (await service.synchronize()).last_attempt.outcome == "diverged"
        # Explicit review authority is simulated here, outside the automatic BM
        # sync. Both complete histories are parents; the UUID manifest is reviewed.
        git(
            "--git-dir",
            sync_peer.path,
            "fetch",
            "--no-tags",
            local.path,
            "refs/heads/main:refs/review/local",
        )
        left, right = local.snapshot(), sync_peer.snapshot()
        manifest = GitProjectManifest(
            project_external_id=left.manifest.project_external_id,
            notes=tuple(
                GitNoteIdentity(path=p, external_id=i)
                for p, i in {**left.manifest.note_ids, **right.manifest.note_ids}.items()
            ),
        )
        tree = sync_peer._write_tree(
            {**left.files, **right.files, GIT_PROJECT_MANIFEST_PATH: manifest.canonical_json()}
        )
        merged = (
            sync_peer._git(
                "commit-tree",
                tree,
                "-p",
                remote_tip,
                "-p",
                local_tip,
                input_bytes=b"Explicitly reviewed independent changes\n",
            )
            .strip()
            .decode()
        )
        sync_peer._git("update-ref", "refs/heads/main", merged, remote_tip)
        recovered = await service.synchronize()
        assert recovered.last_attempt.outcome == "pulled"
        assert local.head() == sync_peer.head() == merged
        assert local.is_ancestor(local_tip, merged) and local.is_ancestor(remote_tip, merged)
        assert set(local.snapshot().files) == {"local.md", "peer.md"}


@pytest.mark.asyncio
async def test_shutdown_keeps_ownership_until_started_push_finishes(
    host_setup, sync_peer, monkeypatch
):
    from basic_memory.api.app import app

    original = GitProjectRepository.push_published
    entered, release = threading.Event(), threading.Event()

    def blocked(self, remote, commit):
        entered.set()
        assert release.wait(20)
        original(self, remote, commit)

    monkeypatch.setattr(GitProjectRepository, "push_published", blocked)
    lifecycle = app.router.lifespan_context(app)
    await lifecycle.__aenter__()
    service = runtime(host_setup.project.external_id).sync
    publish(host_setup.repository, {"shutdown.md": note("Shutdown")})
    transfer = asyncio.create_task(service.synchronize())
    assert await asyncio.to_thread(entered.wait, 10)
    shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0.05)
        assert not shutdown.done() and service.lock.locked()
    finally:
        release.set()
        result = await transfer
        await shutdown
    assert result.last_attempt.outcome == "pushed"
    assert sync_peer.head() == host_setup.repository.head()
    async with app.router.lifespan_context(app):
        assert (await runtime(host_setup.project.external_id).sync.status()).relationship == "equal"


@pytest.mark.asyncio
async def test_foreign_local_note_uuid_is_rejected_before_publication(
    host_setup, sync_peer, config_manager, tmp_path
):
    from basic_memory import db
    from basic_memory.api.app import app
    from basic_memory.config import ProjectEntry
    from basic_memory.repository.project_repository import ProjectRepository

    root = tmp_path / "owner"
    root.mkdir()
    config = host_setup.config
    config.projects["owner"] = ProjectEntry(path=str(root))
    _, factory = await db.get_or_create_db(config.database_path)
    async with db.scoped_session(factory) as session:
        owner = await ProjectRepository().create(
            session, {"name": "owner", "path": str(root), "is_active": True}
        )
    owned = GitProjectRepository.create(
        tmp_path / "owner.git", project_external_id=UUID(owner.external_id)
    )
    publish(owned, {"owned.md": note("Owned")})
    identity = owned.snapshot().manifest.note_ids["owned.md"]
    config.git_repositories[UUID(owner.external_id)] = owned.path
    config_manager.save_config(config)
    base = sync_peer.head()
    prepared = sync_peer.prepare(
        transaction_id=uuid4(),
        base_commit=base,
        files={"copied.md": note("Copied")},
        note_ids={"copied.md": identity},
    )
    sync_peer.publish(prepared)
    async with app.router.lifespan_context(app):
        result = await runtime(host_setup.project.external_id).sync.synchronize()
        assert result.last_attempt.kind == "failed"
        assert result.last_attempt.reason == "invalid_incoming"
        assert host_setup.repository.head() == base
