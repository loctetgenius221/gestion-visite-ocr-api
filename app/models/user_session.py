from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Session ouverte : un refresh token émis et encore vivant.

    Les JWT sont autoportants, donc invisibles du serveur une fois émis. Sans trace
    en base, il est impossible de répondre à « quels appareils sont connectés ? » ni
    de couper l'accès d'une tablette perdue avant l'expiration du token, sept jours
    plus tard. Chaque émission de refresh token crée donc une ligne ici.

    Seul le `jti` est stocké, jamais le token lui-même : une fuite de cette table
    ne permettrait pas de se faire passer pour l'utilisateur.

    Un refresh token n'est accepté que si sa session existe et n'est pas
    révoquée (ADR-023) : c'est ce qui permet de couper réellement une tablette
    perdue. `revoked_tokens` reste alimentée, en défense en profondeur.
    """

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Token remplacé lors du dernier glissement. Il reste utilisable jusqu'à sa
    # propre expiration — un client qui n'a pas enregistré le nouveau ne doit
    # pas être déconnecté — mais **seulement tant que la session vit** :
    # révoquer la session le coupe avec le token courant.
    previous_jti: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Contexte de connexion, pour que l'administrateur reconnaisse l'appareil.
    # Tronqué à 300 caractères : certains User-Agent sont bien plus longs.
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def is_active_at(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<UserSession {self.jti[:8]}… user={self.user_id}>"
