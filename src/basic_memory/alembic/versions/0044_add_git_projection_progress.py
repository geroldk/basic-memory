"""add git projection progress

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-03 21:10:54.083237

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "git_projection_progress",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("projection_key", sa.Text(), nullable=False),
        sa.Column("target_commit", sa.String(40), nullable=False),
        sa.Column("completed_commit", sa.String(40), nullable=True),
        sa.Column("running_commit", sa.String(40), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "stage IN ('content', 'files', 'graph', 'vectors')", name="ck_git_projection_stage"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "stage"),
    )


def downgrade() -> None:
    op.drop_table("git_projection_progress")
