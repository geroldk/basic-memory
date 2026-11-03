"""Stable local bindings, conflicting owners and interrupted marker creation."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from basic_memory.index import git_project_ownership as ownership
from basic_memory.index.git_project_ownership import GitLocalBinding, own_git_project
from basic_memory.models import Project
from basic_memory.repository.git_project_repository import GitProjectRepository
from basic_memory.repository.git_projection_repository import GitProjectionConflict


@pytest.fixture
def owned(test_project, session_maker, tmp_path_factory):
    repository = GitProjectRepository.create(
        tmp_path_factory.mktemp("ownership") / "project.git",
        project_external_id=UUID(test_project.external_id),
    )
    return repository, dict(
        project_id=test_project.id,
        project_external_id=test_project.external_id,
        project_root=Path(test_project.path),
        session_maker=session_maker,
    )


@pytest.mark.asyncio
async def test_binding_is_durable_and_both_root_and_repository_are_exclusive(owned):
    repo, args = owned
    root = args["project_root"]
    async with own_git_project(repo, **args):
        marker = root / ".basic-memory/git-binding.json"
        binding = GitLocalBinding.model_validate_json(marker.read_bytes())
        assert binding.project_external_id == UUID(args["project_external_id"])
        assert binding.project_root == str(root.resolve())
        assert binding.repository_path == str(repo.path)
        with pytest.raises(GitProjectionConflict, match="already running"):
            async with own_git_project(repo, **args):
                pytest.fail("duplicate root owner")
    async with own_git_project(repo, **args):
        assert marker.exists()
    other = GitProjectRepository.create(
        repo.path.parent / "other.git", project_external_id=binding.project_external_id
    )
    with pytest.raises(GitProjectionConflict, match="bound differently"):
        async with own_git_project(other, **args):
            pytest.fail("silent repository replacement")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["missing", "uuid", "root", "repository_uuid", "overlap_root", "overlap_repository"]
)
async def test_registration_and_project_boundaries_are_checked(owned, invalid):
    repo, args = owned
    session_maker = args["session_maker"]
    if invalid == "missing":
        args["project_id"] += 999
    elif invalid == "uuid":
        args["project_external_id"] = str(uuid4())
    elif invalid == "root":
        args["project_root"] = args["project_root"] / "different"
    elif invalid == "repository_uuid":
        repo = GitProjectRepository.create(repo.path.parent / "foreign.git")
    else:
        path = args["project_root"] / "nested" if invalid == "overlap_root" else repo.path.parent
        async with session_maker() as session, session.begin():
            session.add(Project(name="other", path=str(path), is_active=True))
    with pytest.raises(GitProjectionConflict):
        async with own_git_project(repo, **args):
            pytest.fail("invalid ownership binding")


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink", ["directory", "lock", "binding"])
async def test_control_symlinks_are_refused_without_touching_the_target(owned, symlink):
    repo, args = owned
    control = args["project_root"] / ".basic-memory"
    control.parent.mkdir(parents=True, exist_ok=True)
    external = repo.path.parent / "unrelated"
    external.mkdir()
    backup = control.with_name(".basic-memory-before-symlink-test")
    if symlink == "directory":
        control.rename(backup)
        control.symlink_to(external, target_is_directory=True)
    else:
        control.mkdir(exist_ok=True)
        (control / ("git-projection.lock" if symlink == "lock" else "git-binding.json")).symlink_to(
            external / "private"
        )
    try:
        with pytest.raises(GitProjectionConflict, match="symlink"):
            async with own_git_project(repo, **args):
                pytest.fail("unsafe ownership metadata")
    finally:
        if symlink == "directory":
            control.unlink()
            backup.rename(control)
    assert list(external.iterdir()) == []


@pytest.mark.asyncio
async def test_interrupted_first_binding_repairs_only_a_matching_pair(owned, monkeypatch):
    repo, args = owned
    original = ownership.write_file_atomic_bytes

    async def interrupted(path, content):
        if Path(path).parent == repo.path:
            raise OSError("Interrupted second marker")
        await original(path, content)

    monkeypatch.setattr(ownership, "write_file_atomic_bytes", interrupted)
    with pytest.raises(OSError, match="second marker"):
        async with own_git_project(repo, **args):
            pytest.fail("incomplete binding")
    root_marker = args["project_root"] / ".basic-memory/git-binding.json"
    assert root_marker.exists() and not (repo.path / "bm-local-binding.json").exists()
    monkeypatch.setattr(ownership, "write_file_atomic_bytes", original)
    async with own_git_project(repo, **args):
        assert json.loads(root_marker.read_bytes()) == json.loads(
            (repo.path / "bm-local-binding.json").read_bytes()
        )
    async with args["session_maker"]() as session, session.begin():
        await session.execute(
            update(Project)
            .where(Project.id == args["project_id"])
            .values(path=str(args["project_root"] / "new"))
        )
    args["project_root"] /= "new"
    with pytest.raises(GitProjectionConflict, match="bound differently"):
        async with own_git_project(repo, **args):
            pytest.fail("silent root relocation")
    assert not (args["project_root"] / ".basic-memory/git-binding.json").exists()


@pytest.mark.asyncio
async def test_another_project_can_be_owned_while_the_first_is_busy(owned):
    repository, args = owned
    other = GitProjectRepository.create(repository.path.parent / "independent.git")
    manifest = other.snapshot().manifest
    async with args["session_maker"]() as session, session.begin():
        project = Project(
            name="independent",
            external_id=str(manifest.project_external_id),
            path=str(repository.path.parent / "independent-notes"),
            is_active=True,
        )
        session.add(project)
        await session.flush()
        other_id = project.id
    async with own_git_project(repository, **args):
        async with own_git_project(
            other,
            project_id=other_id,
            project_external_id=str(manifest.project_external_id),
            project_root=Path(project.path),
            session_maker=args["session_maker"],
        ):
            with pytest.raises(GitProjectionConflict, match="already running"):
                async with own_git_project(repository, **args):
                    pytest.fail("A duplicate owner in project A must still be refused")
