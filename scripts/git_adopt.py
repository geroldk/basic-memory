"""Adopt existing local projects into Git: plan, then publish.

The adoption primitives (``services/git_project_adoption.py``) assume that every legacy
writer is stopped and drained. This script does not check that; the operator must stop the
MCP service, the rclone timer and any editor first (see docs/BM006_GIT_PUBLICATION.md).

    git_adopt.py --repos-dir DIR plan     [--project NAME ...]   # capture only, writes plans
    git_adopt.py --repos-dir DIR publish  [--project NAME ...]   # publish, then register in config

``plan`` is repeatable and changes no note, no SQL row and no config; it creates the bare
repository and a ``bm-adoption.json`` evidence file next to it. ``publish`` additionally writes
``git_repositories`` into the active profile's ``config.json``. Nothing is registered unless every
selected project was published, because a host with only some projects Git-managed is refused.

The database is opened without running migrations: a profile that is not already at this
version's head is refused, never upgraded here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import UUID

from sqlalchemy import select


async def run(args: argparse.Namespace) -> int:
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    from basic_memory import db
    from basic_memory.config import ConfigManager
    from basic_memory.models import Project
    from basic_memory.repository.git_project_repository import GitProjectRepository
    from basic_memory.services.git_project_adoption import (
        prepare_git_adoption,
        publish_git_adoption,
    )

    manager = ConfigManager()
    config = manager.config
    repos_dir = Path(args.repos_dir).resolve()
    engine, maker = await db.get_or_create_db(
        config.database_path, ensure_migrations=False, config=config
    )

    # --- Refuse a database that this code would have to migrate ---
    async with engine.connect() as connection:
        from sqlalchemy import text

        current = (
            await connection.execute(text("select version_num from alembic_version"))
        ).scalar()
    script_location = Path(db.__file__).parent / "alembic"
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(script_location))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if current not in heads:
        print(
            f"REFUSED: database is at {current}, code head is {heads}; migrate deliberately first"
        )
        return 2

    async with maker() as session:
        projects = list((await session.scalars(select(Project).where(Project.is_active))).all())
    wanted = set(args.project or [])
    selected = [p for p in projects if not wanted or p.name in wanted]
    if wanted and {p.name for p in selected} != wanted:
        print(f"REFUSED: unknown project(s): {sorted(wanted - {p.name for p in selected})}")
        return 2

    # --- Plan or publish each project; collect what would be registered ---
    registered: dict[str, str] = {}
    failed = False
    for project in selected:
        repo_path = repos_dir / f"{project.name}.git"
        try:
            # Reopen a repository left by an earlier plan: both steps must be repeatable.
            if repo_path.exists():
                repository = GitProjectRepository(repo_path)
            else:
                repository = GitProjectRepository.create(
                    repo_path, project_external_id=UUID(project.external_id)
                )
            plan = await prepare_git_adoption(repository, project=project, session_maker=maker)
            if args.command == "publish":
                publication = await publish_git_adoption(
                    repository, project=project, session_maker=maker
                )
                registered[project.external_id] = str(repo_path)
                print(
                    f"{project.name}: published {publication.commit_id[:12]} ({len(plan.notes)} notes)"
                )
            else:
                print(f"{project.name}: plan ok ({len(plan.notes)} notes) -> {repo_path}")
        except Exception as error:  # report every project, then stop before touching config
            failed = True
            print(f"{project.name}: REFUSED {type(error).__name__}: {error}")

    if args.command == "publish":
        if failed:
            print("config NOT changed: not every selected project could be published")
            return 3
        covered = {p.external_id for p in selected}
        if covered != {p.external_id for p in projects}:
            print("config NOT changed: a host with only some projects Git-managed is refused")
            return 3
        path = Path(manager.config_file)
        data = json.loads(path.read_text())
        data["git_repositories"] = {**data.get("git_repositories", {}), **registered}
        path.write_text(json.dumps(data, indent=2))
        print(f"config updated: git_repositories for {len(registered)} projects")
    return 3 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("command", choices=["plan", "publish"])
    parser.add_argument("--repos-dir", required=True, help="where the bare repositories live")
    parser.add_argument("--project", action="append", help="repeatable; default: all active")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
