from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Text, Index, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import SourceType, ProjectStatus
from app.models.id_gen import generate_cuid

if TYPE_CHECKING:
    from app.models.endpoint import Endpoint
    from app.models.documentation_page import DocumentationPage


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_status", "status"),
        Index("ix_projects_created_at", "created_at"),
        Index("ix_projects_user_id", "user_id"),
        {
            **Base.__table_args__,
        },
    )

    id: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=generate_cuid
    )
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_type: Mapped[SourceType] = mapped_column(
        SAEnum(SourceType, native_enum=False, length=20),
        nullable=False,
        default=SourceType.RAW_CODE,
    )
    source_code: Mapped[str] = mapped_column(Text, nullable=False)
    framework: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        SAEnum(ProjectStatus, native_enum=False, length=20),
        nullable=False,
        default=ProjectStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    endpoints: Mapped[list[Endpoint]] = relationship(
        "Endpoint",
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    documentation_pages: Mapped[list[DocumentationPage]] = relationship(
        "DocumentationPage",
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
