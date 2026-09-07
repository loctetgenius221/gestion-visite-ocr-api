from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.referentiel import Service
    from app.models.user import User
    from app.models.visit import Visit


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Structure ou organisation abritant des postes de contrôle, services et visites."""

    __tablename__ = "organizations"

    nom: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    localisation: Mapped[str | None] = mapped_column(String(300), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )

    # --- Personnalisation & Charte Graphique (Branding) ---
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    secondary_color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    accent_color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    theme_config: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    # Relations vers les entités dépendantes
    users: Mapped[list[User]] = relationship("User", back_populates="organization")
    services: Mapped[list[Service]] = relationship("Service", back_populates="organization")
    visits: Mapped[list[Visit]] = relationship("Visit", back_populates="organization")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Organization {self.code}: {self.nom}>"
