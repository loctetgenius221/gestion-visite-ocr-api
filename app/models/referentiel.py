from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Service(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Service / direction du ministère. Hiérarchie optionnelle via `parent_id`."""

    __tablename__ = "services"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    floor: Mapped[str | None] = mapped_column(String(50), nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )

    parent: Mapped[Service | None] = relationship(
        "Service", remote_side="Service.id", back_populates="children"
    )
    children: Mapped[list[Service]] = relationship(
        "Service", back_populates="parent", cascade="save-update"
    )
    agents: Mapped[list[Agent]] = relationship("Agent", back_populates="service")


class Agent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Personnel du ministère susceptible d'être visité (distinct de `User`)."""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(200), nullable=True)
    office: Mapped[str | None] = mapped_column(String(100), nullable=True)
    service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    service: Mapped[Service] = relationship("Service", back_populates="agents")


class Purpose(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Référentiel des motifs de visite."""

    __tablename__ = "purposes"

    libelle: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
