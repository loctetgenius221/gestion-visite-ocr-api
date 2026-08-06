from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RevokedToken
from app.models.user import User


class UserRepository:
    """Accès DB aux comptes utilisateurs."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_identifiant(self, identifiant: str) -> User | None:
        stmt = select(User).where(User.identifiant == identifiant)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user


class RevokedTokenRepository:
    """Liste de révocation des refresh tokens (logout)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def is_revoked(self, jti: str) -> bool:
        stmt = select(RevokedToken.id).where(RevokedToken.jti == jti)
        return (await self.session.execute(stmt)).first() is not None

    async def revoke(self, jti: str, user_id: uuid.UUID, expires_at: datetime) -> None:
        if await self.is_revoked(jti):
            return
        self.session.add(RevokedToken(jti=jti, user_id=user_id, expires_at=expires_at))
        await self.session.flush()

    async def purge_expired(self) -> int:
        """Supprime les entrées dont le token est de toute façon expiré."""
        stmt = delete(RevokedToken).where(RevokedToken.expires_at < datetime.now(UTC))
        result = await self.session.execute(stmt)
        return result.rowcount or 0
