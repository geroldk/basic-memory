"""add git graph reusable basis

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-04 06:07:30.925724

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "git_projection_progress", sa.Column("reusable_commit", sa.String(40), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("git_projection_progress", "reusable_commit")
