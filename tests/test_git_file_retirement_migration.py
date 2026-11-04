"""Upgrade/downgrade the durable cleanup table independently of published notes."""

import sqlite3
from contextlib import closing

from alembic import command

from tests.test_note_content_migration import sqlite_alembic_config


def test_git_file_retirement_migration_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BASIC_MEMORY_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(tmp_path / "config"))
    path = tmp_path / "retirements.db"
    config = sqlite_alembic_config(path)
    command.upgrade(config, "0044")
    command.upgrade(config, "0045")
    with closing(sqlite3.connect(path)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(git_file_retirement)")}
        assert columns == {"project_id", "file_path", "retirement_id", "expected_checksums"}
        foreign_key = connection.execute("PRAGMA foreign_key_list(git_file_retirement)").fetchone()
        assert foreign_key[2] == "project" and foreign_key[6] == "CASCADE"
    command.downgrade(config, "0044")
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='git_file_retirement'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='note_content'"
            ).fetchone()
            is not None
        )
