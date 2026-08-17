from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.models.enums import VisitStatus
from app.models.referentiel import Agent, Purpose, Service
from app.models.user import User
from app.models.visitor import Visitor


class Visit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Passage d'un visiteur : entrée (`PRESENT`) puis sortie (`SORTI`)."""

    __tablename__ = "visits"
    __table_args__ = (
        # Le listing filtre presque toujours sur statut + fenêtre de dates : index composite.
        Index("ix_visits_statut_checked_in_at", "statut", "checked_in_at"),
    )

    visitor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("visitors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("services.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Nullable : beaucoup de visites ne visent personne en particulier — dépôt de
    # dossier, retrait de document, livraison. Le visiteur va **au service**, pas à
    # quelqu'un. Forcer une personne dans ces cas produisait une donnée fausse mais
    # crédible : l'agent d'accueil désignait toujours le même nom (ADR-019).
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # Nullable : l'agent de contrôle peut saisir un motif hors référentiel (`motif_libre`).
    purpose_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("purposes.id", ondelete="SET NULL"), nullable=True
    )
    motif_libre: Mapped[str | None] = mapped_column(String(500), nullable=True)
    badge_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    signature_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    statut: Mapped[VisitStatus] = mapped_column(
        SAEnum(VisitStatus, name="visit_status", values_callable=lambda e: [m.value for m in e]),
        default=VisitStatus.PRESENT,
        nullable=False,
    )
    checked_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    checked_in_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    checked_out_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    # --- Annulation logique (administrateur) ---
    # Une visite erronée n'est jamais supprimée : le registre doit rester complet
    # et vérifiable. Elle passe en `ANNULEE`, motif obligatoire.
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Clé d'idempotence fournie par le client mobile en mode hors-ligne : permet à
    # `POST /visits/sync` de rejouer un batch sans créer de doublon (voir ADR-004).
    client_reference: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True, index=True
    )

    visitor: Mapped[Visitor] = relationship("Visitor", lazy="joined")
    service: Mapped[Service] = relationship("Service", lazy="joined")
    agent: Mapped[Agent | None] = relationship("Agent", lazy="joined")
    purpose: Mapped[Purpose | None] = relationship("Purpose", lazy="joined")
    checked_in_user: Mapped[User] = relationship(
        "User", foreign_keys=[checked_in_by], lazy="joined"
    )
    checked_out_user: Mapped[User | None] = relationship(
        "User", foreign_keys=[checked_out_by], lazy="joined"
    )
    cancelled_user: Mapped[User | None] = relationship(
        "User", foreign_keys=[cancelled_by], lazy="joined"
    )

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<Visit {self.id} {self.statut}>"
