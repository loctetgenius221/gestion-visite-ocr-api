from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow

if TYPE_CHECKING:
    from app.models.organization import Organization


class AutomaticClosureRun(UUIDPrimaryKeyMixin, Base):
    """Journal d'exécution quotidienne de la clôture automatique pour garantir l'idempotence."""

    __tablename__ = "automatic_closure_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "run_date", name="uq_auto_closure_org_date"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    closed_visits_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cases_created_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization: Mapped[Organization | None] = relationship("Organization", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AutomaticClosureRun {self.run_date} org={self.organization_id} "
            f"closed={self.closed_visits_count}>"
        )
