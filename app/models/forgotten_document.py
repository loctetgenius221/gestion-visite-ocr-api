from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ForgottenDocumentStatus

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.visit import Visit
    from app.models.visitor import Visitor


class ForgottenDocumentCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Fiche de suivi d'une pièce d'identité potentiellement oubliée au poste."""

    __tablename__ = "forgotten_document_cases"
    __table_args__ = (
        Index("ix_forgotten_doc_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    visit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("visits.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    visitor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("visitors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[ForgottenDocumentStatus] = mapped_column(
        SAEnum(
            ForgottenDocumentStatus,
            name="forgotten_document_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ForgottenDocumentStatus.PENDING,
        nullable=False,
        index=True,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Réservé pour l'extension SMS (V2)
    sms_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sms_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization | None] = relationship("Organization", lazy="joined")
    visit: Mapped[Visit] = relationship("Visit", lazy="joined")
    visitor: Mapped[Visitor] = relationship("Visitor", lazy="joined")
    resolved_by_user: Mapped[User | None] = relationship(
        "User", foreign_keys=[resolved_by], lazy="joined"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ForgottenDocumentCase {self.id} {self.status}>"
