"""Durable permission to retire an older published file, independent of Entity life."""

from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from basic_memory.models.base import Base


class GitFileRetirement(Base):
    __tablename__ = "git_file_retirement"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), primary_key=True
    )
    file_path: Mapped[str] = mapped_column(String, primary_key=True)
    retirement_id: Mapped[str] = mapped_column(String(36))
    expected_checksums: Mapped[list[str]] = mapped_column(JSON)
