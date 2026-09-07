from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import ArchivableMixin, Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization


class Service(UUIDPrimaryKeyMixin, TimestampMixin, ArchivableMixin, Base):
    """Service / direction d'une organisation. Hiérarchie optionnelle via `parent_id`."""

    __tablename__ = "services"
    __table_args__ = (
        # Le code n'est unique **que dans son organisation** : deux structures ont
        # chacune leur « DRH ». L'index global d'origine a été retiré par la
        # migration multi-organisations sans être remplacé, ce qui autorisait deux
        # services de même code dans une même organisation.
        UniqueConstraint("organization_id", "code", name="uq_services_organization_id_code"),
    )

    # Obligatoire : un service sans organisation n'apparaîtrait dans la liste
    # d'aucun poste de contrôle, tous étant rattachés à une structure.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    floor: Mapped[str | None] = mapped_column(String(50), nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="services")
    parent: Mapped[Service | None] = relationship(
        "Service", remote_side="Service.id", back_populates="children"
    )
    children: Mapped[list[Service]] = relationship(
        "Service", back_populates="parent", cascade="save-update"
    )
    agents: Mapped[list[Agent]] = relationship("Agent", back_populates="service")


class Agent(UUIDPrimaryKeyMixin, TimestampMixin, ArchivableMixin, Base):
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


class Purpose(UUIDPrimaryKeyMixin, TimestampMixin, ArchivableMixin, Base):
    """Référentiel des motifs de visite.

    `organization_id` à `NULL` désigne un motif **commun**, proposé à toutes les
    structures et administré par la plateforme ; renseigné, le motif n'appartient
    qu'à son organisation. Sans cette distinction, le libellé était unique pour
    toute la plateforme : la deuxième structure à vouloir un motif « Réunion » ne
    pouvait pas le créer, et l'archiver chez l'une le retirait à toutes.
    """

    __tablename__ = "purposes"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "libelle", name="uq_purposes_organization_id_libelle"
        ),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    libelle: Mapped[str] = mapped_column(String(200), nullable=False)

    organization: Mapped[Organization | None] = relationship("Organization")
