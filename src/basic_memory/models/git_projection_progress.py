"""Local operational checkpoints, never portable note history."""

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from basic_memory.models.base import Base


class GitProjectionProgress(Base):
    __tablename__ = "git_projection_progress"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('content', 'files', 'graph', 'vectors')", name="ck_git_projection_stage"
        ),
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(String, primary_key=True)
    projection_key: Mapped[str] = mapped_column(Text)
    target_commit: Mapped[str] = mapped_column(String(40))
    completed_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    running_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
