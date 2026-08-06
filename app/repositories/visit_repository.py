from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import DocumentType, VisitStatus
from app.models.visit import Visit
from app.models.visitor import Visitor
from app.schemas.visit import VisitFilters


class VisitorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_document(
        self, type_document: DocumentType, numero_document: str
    ) -> Visitor | None:
        """Un visiteur est identifié par le couple (type de document, numéro de document)."""
        stmt = select(Visitor).where(
            Visitor.type_document == type_document,
            Visitor.numero_document == numero_document,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def add(self, visitor: Visitor) -> Visitor:
        self.session.add(visitor)
        await self.session.flush()
        return visitor


class VisitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, visit: Visit) -> Visit:
        self.session.add(visit)
        await self.session.flush()
        return visit

    async def get_by_id(self, visit_id: uuid.UUID) -> Visit | None:
        # `populate_existing` force le rafraîchissement de l'instance déjà présente
        # dans l'identity map : sans lui, une relation chargée avant modification
        # (ex. `checked_out_user` encore nul avant la clôture) resterait périmée.
        stmt = (
            select(Visit)
            .where(Visit.id == visit_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).unique().scalars().first()

    async def get_by_client_reference(self, client_reference: str) -> Visit | None:
        stmt = select(Visit).where(Visit.client_reference == client_reference)
        return (await self.session.execute(stmt)).unique().scalars().first()

    async def find_open_visit(self, visitor_id: uuid.UUID) -> Visit | None:
        """Visite encore ouverte pour ce visiteur — sert à détecter les doublons d'entrée."""
        stmt = select(Visit).where(
            Visit.visitor_id == visitor_id, Visit.statut == VisitStatus.PRESENT
        )
        return (await self.session.execute(stmt)).unique().scalars().first()

    @staticmethod
    def _apply_filters(stmt: Select, filters: VisitFilters) -> Select:
        if filters.statut is not None:
            stmt = stmt.where(Visit.statut == filters.statut)
        if filters.date_from is not None:
            stmt = stmt.where(Visit.checked_in_at >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(Visit.checked_in_at <= filters.date_to)
        if filters.search:
            pattern = f"%{filters.search.strip().lower()}%"
            stmt = stmt.join(Visitor, Visit.visitor_id == Visitor.id).where(
                or_(
                    func.lower(Visitor.nom).like(pattern),
                    func.lower(Visitor.prenom).like(pattern),
                    func.lower(Visitor.numero_document).like(pattern),
                )
            )
        return stmt

    async def count(self, filters: VisitFilters) -> int:
        stmt = self._apply_filters(select(func.count(Visit.id)).select_from(Visit), filters)
        return (await self.session.execute(stmt)).scalar_one()

    async def list_paginated(
        self, filters: VisitFilters, *, limit: int, offset: int
    ) -> list[Visit]:
        stmt = self._apply_filters(select(Visit), filters)
        order = Visit.checked_in_at.asc() if filters.sort == "asc" else Visit.checked_in_at.desc()
        stmt = stmt.order_by(order, Visit.id).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).unique().scalars().all())

    # --- Statistiques dashboard ---

    async def count_checked_in_between(self, start: datetime, end: datetime) -> int:
        stmt = select(func.count(Visit.id)).where(
            Visit.checked_in_at >= start, Visit.checked_in_at < end
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_checked_out_between(self, start: datetime, end: datetime) -> int:
        stmt = select(func.count(Visit.id)).where(
            Visit.checked_out_at.is_not(None),
            Visit.checked_out_at >= start,
            Visit.checked_out_at < end,
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def count_present(self) -> int:
        stmt = select(func.count(Visit.id)).where(Visit.statut == VisitStatus.PRESENT)
        return (await self.session.execute(stmt)).scalar_one()

    async def closed_visits_durations_seconds(self, start: datetime, end: datetime) -> list[float]:
        """Durées des visites clôturées sur la période, pour la moyenne du dashboard.

        Le calcul de la différence de dates est fait en Python plutôt qu'en SQL :
        les fonctions d'intervalle diffèrent entre PostgreSQL et SQLite (base de test).
        """
        stmt = select(Visit.checked_in_at, Visit.checked_out_at).where(
            Visit.checked_out_at.is_not(None),
            Visit.checked_out_at >= start,
            Visit.checked_out_at < end,
        )
        rows = (await self.session.execute(stmt)).all()
        return [(out - inn).total_seconds() for inn, out in rows if out is not None]
