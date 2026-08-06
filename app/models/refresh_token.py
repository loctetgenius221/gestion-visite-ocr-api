from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RevokedToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Liste de révocation des refresh tokens (logout).

    Redis n'étant pas déployé pour l'instant, la blacklist vit en base — l'option
    est explicitement autorisée par la spec (§5.1). Voir ADR-002.
    Les entrées expirées peuvent être purgées via `RevokedTokenRepository.purge_expired`.
    """

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
