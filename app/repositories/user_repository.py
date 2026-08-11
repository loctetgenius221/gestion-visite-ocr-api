from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole, UserStatus
from app.models.refresh_token import RevokedToken
from app.models.user import User
from app.models.user_session import UserSession
from app.schemas.user import UserFilters


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

    @staticmethod
    def _apply_filters(stmt: Select, filters: UserFilters) -> Select:
        if filters.role is not None:
            stmt = stmt.where(User.role == filters.role)
        if filters.status is not None:
            stmt = stmt.where(User.is_active.is_(filters.status is UserStatus.ACTIVE))
        if filters.search:
            pattern = f"%{filters.search.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(User.nom).like(pattern),
                    func.lower(User.identifiant).like(pattern),
                )
            )
        return stmt

    async def count(self, filters: UserFilters) -> int:
        stmt = self._apply_filters(select(func.count(User.id)).select_from(User), filters)
        return (await self.session.execute(stmt)).scalar_one()

    async def list_paginated(
        self, filters: UserFilters, *, limit: int, offset: int
    ) -> list[User]:
        stmt = self._apply_filters(select(User), filters)
        order = User.created_at.asc() if filters.sort == "asc" else User.created_at.desc()
        # `User.id` en second critère : sans lui, deux comptes créés dans la même
        # transaction partagent leur `created_at` et la pagination peut en oublier
        # un tout en en montrant un autre deux fois.
        stmt = stmt.order_by(order, User.id).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_active_admins(self, *, excluding: uuid.UUID | None = None) -> int:
        """Compte les administrateurs actifs, hors compte exclu.

        Sert au garde-fou anti-verrouillage : refuser de désactiver ou de
        rétrograder le dernier administrateur.
        """
        stmt = select(func.count(User.id)).where(
            User.role == UserRole.ADMIN, User.is_active.is_(True)
        )
        if excluding is not None:
            stmt = stmt.where(User.id != excluding)
        return (await self.session.execute(stmt)).scalar_one()


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


class UserSessionRepository:
    """Sessions ouvertes : refresh tokens émis, pour les lister et les couper.

    La table `revoked_tokens` reste l'autorité sur la validité d'un token ; celle-ci
    n'existe que pour rendre les sessions **visibles** et révocables à distance.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def open(
        self,
        *,
        user_id: uuid.UUID,
        jti: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> UserSession:
        record = UserSession(
            user_id=user_id,
            jti=jti,
            expires_at=expires_at,
            # Tronqué plutôt que rejeté : un User-Agent exotique ne doit pas faire
            # échouer une authentification par ailleurs valide.
            user_agent=(user_agent or "")[:300] or None,
            ip_address=(ip_address or "")[:64] or None,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_by_id(self, session_id: uuid.UUID) -> UserSession | None:
        return await self.session.get(UserSession, session_id)

    async def get_by_jti(self, jti: str) -> UserSession | None:
        stmt = select(UserSession).where(UserSession.jti == jti)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active(self, user_id: uuid.UUID, now: datetime) -> list[UserSession]:
        stmt = (
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
            .order_by(UserSession.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def touch(self, jti: str, now: datetime) -> None:
        """Note l'usage d'une session lors d'un rafraîchissement de token."""
        record = await self.get_by_jti(jti)
        if record is not None:
            record.last_used_at = now

    async def rotate(
        self, record: UserSession, *, jti: str, expires_at: datetime, now: datetime
    ) -> None:
        """Fait glisser la session sur un nouveau refresh token, **sur la même ligne**.

        Créer une ligne par renouvellement ferait enfler la table — un poste
        d'accueil rafraîchit son token toutes les 30 minutes — et noierait la liste
        des sessions actives du dashboard sous des doublons du même appareil.

        L'ancien `jti` n'est pas révoqué : un client qui ignore le nouveau token
        continue de fonctionner jusqu'à l'expiration de l'ancien. C'est ce qui rend
        le glissement rétro-compatible (voir ADR-015).
        """
        record.jti = jti
        record.expires_at = expires_at
        record.last_used_at = now
        await self.session.flush()

    async def mark_revoked(self, record: UserSession, now: datetime) -> None:
        if record.revoked_at is None:
            record.revoked_at = now
            await self.session.flush()

    async def revoke_by_jti(self, jti: str, now: datetime) -> None:
        record = await self.get_by_jti(jti)
        if record is not None:
            await self.mark_revoked(record, now)

    async def list_active_jtis(
        self, user_id: uuid.UUID, now: datetime
    ) -> list[tuple[str, datetime]]:
        """`(jti, expires_at)` des sessions vivantes — pour tout révoquer d'un coup."""
        stmt = select(UserSession.jti, UserSession.expires_at).where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        return [(jti, expires_at) for jti, expires_at in (await self.session.execute(stmt)).all()]
