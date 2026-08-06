from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Compte d'un agent de contrôle (authentification backend)."""

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

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<User {self.identifiant}>"
