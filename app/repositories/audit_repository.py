from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.schemas.audit import AuditLogFilters


class AuditLogRepository:
    """Journal d'audit : écriture par l'application, lecture par l'administrateur.

    Aucune méthode de mise à jour ni de suppression n'est exposée : une trace
    modifiable ne vaut rien lors d'un audit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, log: AuditLog) -> AuditLog:
        self.session.add(log)
        await self.session.flush()
        return log

    @staticmethod
    def _apply_filters(stmt: Select, filters: AuditLogFilters) -> Select:
        if filters.actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == filters.actor_id)
        if filters.action:
            # Préfixe plutôt qu'égalité stricte : `action=visit` doit ramener
            # `visit.created`, `visit.cancelled`, etc.
            stmt = stmt.where(AuditLog.action.startswith(filters.action))
        if filters.entity:
            stmt = stmt.where(AuditLog.entity == filters.entity)
        if filters.entity_id:
            stmt = stmt.where(AuditLog.entity_id == filters.entity_id)
        if filters.date_from is not None:
            stmt = stmt.where(AuditLog.created_at >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(AuditLog.created_at <= filters.date_to)
        return stmt

    async def count(self, filters: AuditLogFilters) -> int:
        stmt = self._apply_filters(select(func.count(AuditLog.id)).select_from(AuditLog), filters)
        return (await self.session.execute(stmt)).scalar_one()

    async def list_paginated(
        self, filters: AuditLogFilters, *, limit: int, offset: int
    ) -> list[AuditLog]:
        stmt = self._apply_filters(select(AuditLog), filters)
        stmt = (
            stmt.order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def distinct_actions(self) -> list[str]:
        """Actions réellement présentes — alimente le filtre du dashboard."""
        stmt = select(AuditLog.action).distinct().order_by(AuditLog.action)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_recent_for_entity(
        self, entity: str, entity_id: str, *, limit: int = 20
    ) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity == entity, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())
