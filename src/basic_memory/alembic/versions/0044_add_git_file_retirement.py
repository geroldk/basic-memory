"""add git file retirement

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-03 22:16:51.467698

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
        "git_file_retirement",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("retirement_id", sa.String(36), nullable=False),
        sa.Column("expected_checksums", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "file_path"),
    )


def downgrade() -> None:
    op.drop_table("git_file_retirement")
