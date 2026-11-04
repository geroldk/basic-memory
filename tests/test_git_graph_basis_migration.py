"""Existing historical completion is not evidence of a reusable physical graph."""

import sqlite3
from contextlib import closing

from alembic import command

from tests.test_note_content_migration import sqlite_alembic_config


def test_graph_basis_migration_retains_completion_but_requires_a_full_first_pass(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BASIC_MEMORY_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(tmp_path / "config"))
    path = tmp_path / "graph.db"
    config = sqlite_alembic_config(path)
    command.upgrade(config, "0044")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO project (id, external_id, name, path, is_active, is_default, permalink, "
            "partition_position, created_at, updated_at) VALUES "
            "(42, 'project-42', 'graph', '/tmp/graph', 1, 0, 'graph', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO git_projection_progress (project_id, stage, projection_key, "
            "target_commit, completed_commit, attempts) VALUES (42, 'graph', '1', ?, ?, 1)",
            ("a" * 40, "a" * 40),
        )
        connection.commit()
    command.upgrade(config, "0045")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT completed_commit, reusable_commit FROM git_projection_progress"
        ).fetchone() == ("a" * 40, None)
    command.downgrade(config, "0044")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM project WHERE id=42").fetchone() == ("graph",)
        assert connection.execute(
            "SELECT completed_commit FROM git_projection_progress"
        ).fetchone() == ("a" * 40,)
