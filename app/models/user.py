from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole, UserStatus

if TYPE_CHECKING:
    from app.models.organization import Organization


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Compte applicatif : agent de contrôle sur mobile, ou administrateur du dashboard."""

    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    nom: Mapped[str] = mapped_column(String(150), nullable=False)
    identifiant: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    mot_de_passe_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    poste: Mapped[str | None] = mapped_column(String(150), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        default=UserRole.AGENT_CONTROLE,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Verrouillage après échecs d'authentification ---
    # Compteur remis à zéro par une connexion réussie ou par un déblocage admin.
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Instant jusqu'auquel toute tentative est refusée. `None` = compte non verrouillé.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization | None] = relationship(
        "Organization", back_populates="users", lazy="joined"
    )

    @property
    def status(self) -> UserStatus:
        """Projection de `is_active` pour l'API d'administration."""
        return UserStatus.ACTIVE if self.is_active else UserStatus.INACTIVE

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now

    @property
    def is_platform_admin(self) -> bool:
        """Administrateur de la plateforme : le seul compte non rattaché."""
        return self.role is UserRole.ADMIN

    @property
    def is_org_admin(self) -> bool:
        return self.role is UserRole.ORG_ADMIN

    @property
    def is_admin(self) -> bool:
        """Porte des droits d'administration, quel qu'en soit le périmètre."""
        return self.role in (UserRole.ADMIN, UserRole.ORG_ADMIN)

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<User {self.identifiant}>"
