"""Service de clôture automatique des visites et de suivi des pièces oubliées.

Ce service est utilisé :
1. Par le script CLI `python -m app.auto_close_visits` (cron/timer systemd).
2. Par le routeur `forgotten_documents` pour la résolution manuelle des fiches.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ForbiddenError,
    ForgottenDocumentAlreadyResolvedError,
    ForgottenDocumentCaseNotFoundError,
)
from app.core.logging import get_logger
from app.models.automatic_closure_run import AutomaticClosureRun
from app.models.base import ensure_utc
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


def _derniere_limite(maintenant: datetime, heure: time) -> datetime:
    """Heure limite la plus récente déjà atteinte : aujourd'hui, ou hier si pas encore."""
    aujourd_hui = datetime.combine(maintenant.date(), heure, tzinfo=UTC)
    return aujourd_hui if aujourd_hui <= maintenant else aujourd_hui - timedelta(days=1)


def _premiere_limite_apres(entree: datetime, heure: time) -> datetime:
    """Première heure limite postérieure à l'entrée : celle du jour, ou du lendemain."""
    meme_jour = datetime.combine(entree.date(), heure, tzinfo=UTC)
    return meme_jour if entree < meme_jour else meme_jour + timedelta(days=1)


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
        now: datetime | None = None,
    ) -> dict:
        """Clôture les visites PRESENT dont l'heure limite est passée.

        Une visite est due à la **première heure limite qui suit son entrée** :
        entrée à 10 h, elle l'est à 17 h le jour même ; entrée à 18 h, le
        lendemain à 17 h. Elle est clôturée **à cette heure-là**, pas à l'heure
        d'exécution de la tâche.

        La tâche est rejouable à volonté : elle ne touche qu'aux visites encore
        ouvertes et dues. C'est ce qui permet de rattraper une visite arrivée en
        retard par la synchro hors-ligne (ADR-025) — saisie à 10 h sans réseau,
        synchronisée à 18 h, elle est clôturée au passage suivant, à 17 h, au lieu
        de rester présente jusqu'au lendemain et d'afficher trente heures de visite.
        """
        now = ensure_utc(now or datetime.now(UTC))
        hour, minute = map(int, closure_time_str.split(":"))
        heure_limite = time(hour, minute)
        limite = _derniere_limite(now, heure_limite)

        stmt = select(Visit).where(
            Visit.statut == VisitStatus.PRESENT,
            Visit.checked_in_at < limite,
        )
        if organization_id is not None:
            stmt = stmt.where(Visit.organization_id == organization_id)

        visits = list((await self.session.execute(stmt)).scalars().all())
        run_date = limite.date()

        if dry_run:
            return {
                "status": "dry_run",
                "run_date": run_date.isoformat(),
                "visits_to_close": len(visits),
            }
        if not visits:
            return {
                "status": "nothing_to_close",
                "run_date": run_date.isoformat(),
                "closed_visits_count": 0,
                "cases_created_count": 0,
            }

        cases_created = 0

        for visit in visits:
            visit.statut = VisitStatus.SORTI
            visit.checked_out_at = _premiere_limite_apres(
                ensure_utc(visit.checked_in_at), heure_limite
            )
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
                organization_id=visit.organization_id,
                metadata={"visit_id": str(visit.id), "status": initial_status.value},
            )

        # Une ligne par organisation et par jour : les passages de rattrapage
        # cumulent leurs compteurs sur celle de l'heure limite traitée.
        run = await self.runs.get_run(organization_id, run_date)
        if run is None:
            run = AutomaticClosureRun(
                organization_id=organization_id,
                run_date=run_date,
                closed_visits_count=len(visits),
                cases_created_count=cases_created,
                executed_at=now,
            )
            await self.runs.add(run)
        else:
            run.closed_visits_count += len(visits)
            run.cases_created_count += cases_created
            run.executed_at = now

        # Audit groupé
        await self.audit.record(
            AuditAction.VISIT_AUTO_CLOSED,
            entity="automatic_closure_run",
            entity_id=run.id,
            actor=None,
            organization_id=organization_id,
            metadata={
                "run_date": run_date.isoformat(),
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
            "run_date": run_date.isoformat(),
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
        if refreshed is None:  # invariant : un `assert` disparaît sous `python -O`
            raise RuntimeError("Invariant violé : refreshed est None.")
        return refreshed
