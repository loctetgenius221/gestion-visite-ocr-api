"""Repository pour les fiches de pièces d'identité potentiellement oubliées et les
exécutions de clôture automatique."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.automatic_closure_run import AutomaticClosureRun
from app.models.forgotten_document import ForgottenDocumentCase
from app.models.visitor import Visitor
from app.schemas.forgotten_document import ForgottenDocumentFilters


class ForgottenDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, case: ForgottenDocumentCase) -> ForgottenDocumentCase:
        self.session.add(case)
        await self.session.flush()
        return case

    async def get_by_id(self, case_id: uuid.UUID) -> ForgottenDocumentCase | None:
        stmt = (
            select(ForgottenDocumentCase)
            .options(
                joinedload(ForgottenDocumentCase.visitor),
                joinedload(ForgottenDocumentCase.visit),
                joinedload(ForgottenDocumentCase.resolved_by_user),
            )
            .where(ForgottenDocumentCase.id == case_id)
        )
        return (await self.session.execute(stmt)).unique().scalars().first()

    async def get_by_visit_id(self, visit_id: uuid.UUID) -> ForgottenDocumentCase | None:
        stmt = select(ForgottenDocumentCase).where(ForgottenDocumentCase.visit_id == visit_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def count(self, filters: ForgottenDocumentFilters) -> int:
        stmt = select(func.count(ForgottenDocumentCase.id))
        stmt = self._apply_filters(stmt, filters)
        return (await self.session.execute(stmt)).scalar() or 0

    async def list_paginated(
        self, filters: ForgottenDocumentFilters, *, limit: int, offset: int
    ) -> list[ForgottenDocumentCase]:
        stmt = (
            select(ForgottenDocumentCase)
            .options(
                joinedload(ForgottenDocumentCase.visitor),
                joinedload(ForgottenDocumentCase.visit),
                joinedload(ForgottenDocumentCase.resolved_by_user),
            )
        )
        stmt = self._apply_filters(stmt, filters)

        colonne = ForgottenDocumentCase.created_at
        order = colonne.asc() if filters.sort == "asc" else colonne.desc()
        stmt = stmt.order_by(order).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).unique().scalars().all())

    @staticmethod
    def _apply_filters(stmt, filters: ForgottenDocumentFilters):
        if filters.organization_id is not None:
            stmt = stmt.where(ForgottenDocumentCase.organization_id == filters.organization_id)
        if filters.status is not None:
            stmt = stmt.where(ForgottenDocumentCase.status == filters.status)
        if filters.date_from is not None:
            stmt = stmt.where(ForgottenDocumentCase.created_at >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(ForgottenDocumentCase.created_at <= filters.date_to)
        if filters.search:
            pattern = f"%{filters.search}%"
            stmt = stmt.join(ForgottenDocumentCase.visitor).where(
                Visitor.nom.ilike(pattern)
                | Visitor.prenom.ilike(pattern)
                | Visitor.numero_document.ilike(pattern)
            )
        return stmt


class AutomaticClosureRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_run(
        self, organization_id: uuid.UUID | None, run_date: date
    ) -> AutomaticClosureRun | None:
        stmt = select(AutomaticClosureRun).where(
            AutomaticClosureRun.run_date == run_date,
            AutomaticClosureRun.organization_id == organization_id,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def add(self, run: AutomaticClosureRun) -> AutomaticClosureRun:
        self.session.add(run)
        await self.session.flush()
        return run
