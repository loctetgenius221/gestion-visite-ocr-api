"""Service de clôture automatique des visites et de suivi des pièces oubliées.

Ce service est utilisé :
1. Par le script CLI `python -m app.auto_close_visits` (cron/timer systemd).
2. Par le routeur `forgotten_documents` pour la résolution manuelle des fiches.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ForbiddenError,
    ForgottenDocumentAlreadyResolvedError,
    ForgottenDocumentCaseNotFoundError,
)
from app.core.logging import get_logger
from app.models.automatic_closure_run import AutomaticClosureRun
from app.models.enums import CheckoutOrigin, ForgottenDocumentStatus, VisitStatus
from app.models.forgotten_document import ForgottenDocumentCase
from app.models.user import User
from app.models.visit import Visit
from app.repositories.forgotten_document_repository import (
    AutomaticClosureRunRepository,
    ForgottenDocumentRepository,
)
from app.schemas.common import Page, PaginationParams
from app.schemas.forgotten_document import (
    ForgottenDocumentCaseRead,
    ForgottenDocumentCaseResolve,
    ForgottenDocumentFilters,
)
from app.services.audit_service import AuditAction, AuditService

logger = get_logger(__name__)


class AutoClosureService:
    """Clôture automatique quotidienne et gestion des pièces oubliées."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.docs = ForgottenDocumentRepository(session)
        self.runs = AutomaticClosureRunRepository(session)
        self.audit = AuditService(session)

    # --- Clôture automatique ---

    async def run_automatic_closure(
        self,
        *,
        closure_time_str: str = "17:00",
        organization_id: uuid.UUID | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Clôture les visites PRESENT créées avant l'heure limite.

        Idempotent : une seconde exécution le même jour pour la même organisation
        est un no-op enregistré.
        """
        today = date.today()

        # Vérifier idempotence
        existing_run = await self.runs.get_run(organization_id, today)
        if existing_run is not None:
            logger.info(
                "Clôture déjà exécutée pour %s le %s (%d visites clôturées)",
                organization_id or "global",
                today,
                existing_run.closed_visits_count,
            )
            return {
                "status": "already_run",
                "run_date": today.isoformat(),
                "closed_visits_count": existing_run.closed_visits_count,
                "cases_created_count": existing_run.cases_created_count,
            }

        # Calculer l'heure limite
        hour, minute = map(int, closure_time_str.split(":"))
        cutoff = datetime.combine(today, time(hour, minute), tzinfo=UTC)
        now = datetime.now(UTC)
        if now < cutoff:
            return {
                "status": "not_due",
                "run_date": today.isoformat(),
                "visits_to_close": 0,
            }

        # Récupérer les visites PRESENT à clôturer
        stmt = select(Visit).where(
            Visit.statut == VisitStatus.PRESENT,
            Visit.checked_in_at < cutoff,
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)

        visits = list((await self.session.execute(stmt)).scalars().all())

        if dry_run:
            return {
                "status": "dry_run",
                "run_date": today.isoformat(),
                "visits_to_close": len(visits),
            }

        cases_created = 0

        for visit in visits:
            visit.statut = VisitStatus.SORTI
            visit.checked_out_at = now
            visit.checkout_origin = CheckoutOrigin.AUTOMATIC

            # Créer la fiche de pièce oubliée
            phone = getattr(visit.visitor, "telephone", None) if visit.visitor else None
            initial_status = (
                ForgottenDocumentStatus.NO_PHONE if not phone else ForgottenDocumentStatus.PENDING
            )
            case = ForgottenDocumentCase(
                organization_id=visit.organization_id,
                visit_id=visit.id,
                visitor_id=visit.visitor_id,
                status=initial_status,
            )
            await self.docs.add(case)
            cases_created += 1
            await self.audit.record(
                AuditAction.FORGOTTEN_DOC_CREATED,
                entity="forgotten_document_case",
                entity_id=case.id,
                actor=None,
                metadata={"visit_id": str(visit.id), "status": initial_status.value},
            )

        # Enregistrer l'exécution
        run = AutomaticClosureRun(
            organization_id=organization_id,
            run_date=today,
            closed_visits_count=len(visits),
            cases_created_count=cases_created,
            executed_at=now,
        )
        await self.runs.add(run)

        # Audit groupé
        await self.audit.record(
            AuditAction.VISIT_AUTO_CLOSED,
            entity="automatic_closure_run",
            entity_id=run.id,
            actor=None,
            metadata={
                "run_date": today.isoformat(),
                "organization_id": str(organization_id) if organization_id else None,
                "closed_visits_count": len(visits),
                "cases_created_count": cases_created,
                "closure_time": closure_time_str,
            },
        )

        await self.session.commit()

        logger.info(
            "Clôture automatique terminée : %d visites clôturées, %d fiches créées",
            len(visits),
            cases_created,
        )
        return {
            "status": "completed",
            "run_date": today.isoformat(),
            "closed_visits_count": len(visits),
            "cases_created_count": cases_created,
        }

    # --- Gestion des pièces oubliées ---

    async def list_forgotten_cases(
        self, filters: ForgottenDocumentFilters, pagination: PaginationParams
    ) -> Page[ForgottenDocumentCaseRead]:
        total = await self.docs.count(filters)
        items = await self.docs.list_paginated(
            filters, limit=pagination.page_size, offset=pagination.offset
        )
        return Page[ForgottenDocumentCaseRead](
            items=[ForgottenDocumentCaseRead.model_validate(item) for item in items],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )

    async def get_case(
        self, case_id: uuid.UUID, organization_id: uuid.UUID | None = None
    ) -> ForgottenDocumentCase:
        case = await self.docs.get_by_id(case_id)
        if case is None:
            raise ForgottenDocumentCaseNotFoundError(details={"case_id": str(case_id)})
        if organization_id is not None and case.organization_id != organization_id:
            raise ForbiddenError("Vous n'avez pas accès à cette fiche.")
        return case

    async def resolve_case(
        self,
        case_id: uuid.UUID,
        payload: ForgottenDocumentCaseResolve,
        current_user: User,
    ) -> ForgottenDocumentCase:
        case = await self.get_case(case_id, current_user.organization_id)

        if case.status == ForgottenDocumentStatus.RESOLVED:
            raise ForgottenDocumentAlreadyResolvedError(
                details={"case_id": str(case_id), "resolved_at": str(case.resolved_at)}
            )

        case.status = ForgottenDocumentStatus.RESOLVED
        case.resolved_at = datetime.now(UTC)
        case.resolved_by = current_user.id
        case.resolution_note = payload.resolution_note

        await self.audit.record(
            AuditAction.FORGOTTEN_DOC_RESOLVED,
            entity="forgotten_document_case",
            entity_id=case.id,
            actor=current_user,
            metadata={
                "visitor_id": str(case.visitor_id),
                "visit_id": str(case.visit_id),
                "resolution_note": payload.resolution_note,
            },
        )
        await self.session.commit()

        refreshed = await self.docs.get_by_id(case.id)
        assert refreshed is not None
        return refreshed
