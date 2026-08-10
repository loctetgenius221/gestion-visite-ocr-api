from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole, UserStatus


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Compte applicatif : agent de contrôle sur mobile, ou administrateur du dashboard."""

    __tablename__ = "users"

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

    @property
    def status(self) -> UserStatus:
        """Projection de `is_active` pour l'API d'administration."""
        return UserStatus.ACTIVE if self.is_active else UserStatus.INACTIVE

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<User {self.identifiant}>"
