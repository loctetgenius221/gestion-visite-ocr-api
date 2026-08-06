from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AgentNotFoundError,
    AppError,
    ConflictError,
    DuplicateVisitError,
    PurposeNotFoundError,
    ServiceNotFoundError,
    VisitAlreadyClosedError,
    VisitNotFoundError,
)
from app.core.logging import get_logger
from app.models.enums import VisitStatus
from app.models.user import User
from app.models.visit import Visit
from app.models.visitor import Visitor
from app.repositories.referentiel_repository import (
    AgentRepository,
    PurposeRepository,
    ServiceRepository,
)
from app.repositories.visit_repository import VisitorRepository, VisitRepository
from app.schemas.common import Page, PaginationParams
from app.schemas.visit import (
    VisitCreate,
    VisitFilters,
    VisitorInput,
    VisitRead,
    VisitSyncItemResult,
    VisitSyncResponse,
)

logger = get_logger(__name__)


class VisitService:
    """Logique métier des visites : enregistrement, listing, clôture, synchro offline."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.visits = VisitRepository(session)
        self.visitors = VisitorRepository(session)
        self.services = ServiceRepository(session)
        self.agents = AgentRepository(session)
        self.purposes = PurposeRepository(session)

    async def create_visit(self, payload: VisitCreate, current_user: User) -> Visit:
        """Crée une visite et commit. Les référentiels sont validés avant insertion."""
        visit = await self._build_visit(payload, current_user.id)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            # Seule contrainte d'unicité pouvant sauter ici : `client_reference`.
            raise DuplicateVisitError(
                details={"client_reference": payload.client_reference}
            ) from exc

        refreshed = await self.visits.get_by_id(visit.id)
        assert refreshed is not None
        return refreshed

    async def _build_visit(self, payload: VisitCreate, user_id: uuid.UUID) -> Visit:
        """Valide les références et prépare la visite en session (sans commit).

        On reçoit l'`user_id` et non l'objet `User` : un rollback expire toutes les
        instances de la session, et relire un attribut expiré déclencherait une IO
        synchrone interdite ici (cas de la synchro batch, qui rollback entre items).
        """
        if payload.client_reference:
            existing = await self.visits.get_by_client_reference(payload.client_reference)
            if existing is not None:
                raise DuplicateVisitError(
                    details={
                        "client_reference": payload.client_reference,
                        "visit_id": str(existing.id),
                    }
                )

        if not await self.services.exists(payload.service_id):
            raise ServiceNotFoundError(details={"service_id": str(payload.service_id)})

        agent = await self.agents.get_by_id(payload.agent_id)
        if agent is None:
            raise AgentNotFoundError(details={"agent_id": str(payload.agent_id)})
        if agent.service_id != payload.service_id:
            raise ConflictError(
                "L'agent sélectionné n'appartient pas au service indiqué.",
                error_code="AGENT_SERVICE_MISMATCH",
                details={"agent_id": str(agent.id), "service_id": str(payload.service_id)},
            )

        if payload.purpose_id is not None and not await self.purposes.exists(payload.purpose_id):
            raise PurposeNotFoundError(details={"purpose_id": str(payload.purpose_id)})

        visitor = await self._upsert_visitor(payload.visitor)

        visit = Visit(
            visitor_id=visitor.id,
            service_id=payload.service_id,
            agent_id=payload.agent_id,
            purpose_id=payload.purpose_id,
            motif_libre=payload.motif_libre,
            badge_number=payload.badge_number,
            signature_url=payload.signature_url,
            statut=VisitStatus.PRESENT,
            checked_in_at=payload.checked_in_at or datetime.now(UTC),
            checked_in_by=user_id,
            client_reference=payload.client_reference,
        )
        return await self.visits.add(visit)

    async def _upsert_visitor(self, data: VisitorInput) -> Visitor:
        """Réutilise le visiteur existant (même document) et rafraîchit ses coordonnées.

        Un même visiteur revient régulièrement au poste d'accueil : dupliquer sa fiche
        à chaque passage fausserait la recherche et l'historique.
        """
        existing = await self.visitors.get_by_document(data.type_document, data.numero_document)
        payload = data.model_dump(exclude_unset=False)
        if data.email is not None:
            payload["email"] = str(data.email)

        if existing is not None:
            for field, value in payload.items():
                # On ne réécrit jamais une valeur connue avec un `None` venu du client.
                if value is not None:
                    setattr(existing, field, value)
            await self.session.flush()
            return existing

        return await self.visitors.add(Visitor(**payload))

    async def get_visit(self, visit_id: uuid.UUID) -> Visit:
        visit = await self.visits.get_by_id(visit_id)
        if visit is None:
            raise VisitNotFoundError(details={"visit_id": str(visit_id)})
        return visit

    async def list_visits(
        self, filters: VisitFilters, pagination: PaginationParams
    ) -> Page[VisitRead]:
        total = await self.visits.count(filters)
        items = await self.visits.list_paginated(
            filters, limit=pagination.page_size, offset=pagination.offset
        )
        return Page[VisitRead](
            items=[VisitRead.model_validate(item) for item in items],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )

    async def checkout(self, visit_id: uuid.UUID, current_user: User) -> Visit:
        visit = await self.get_visit(visit_id)
        if visit.statut is VisitStatus.SORTI:
            raise VisitAlreadyClosedError(
                details={"visit_id": str(visit_id), "checked_out_at": visit.checked_out_at}
            )

        visit.statut = VisitStatus.SORTI
        visit.checked_out_at = datetime.now(UTC)
        visit.checked_out_by = current_user.id
        await self.session.commit()

        refreshed = await self.visits.get_by_id(visit.id)
        assert refreshed is not None
        return refreshed

    async def sync_visits(
        self, payloads: list[VisitCreate], current_user: User
    ) -> VisitSyncResponse:
        """Insère un batch de visites créées hors-ligne, item par item.

        Chaque élément est commité indépendamment : un doublon ou une référence
        invalide au milieu du batch ne doit pas faire perdre les visites valides.
        """
        results: list[VisitSyncItemResult] = []
        # Résolu une seule fois : l'objet `User` serait expiré par le premier rollback.
        user_id = current_user.id

        for index, payload in enumerate(payloads):
            try:
                visit = await self._build_visit(payload, user_id)
                await self.session.commit()
                results.append(
                    VisitSyncItemResult(
                        index=index,
                        client_reference=payload.client_reference,
                        status="created",
                        visit_id=visit.id,
                    )
                )
            except AppError as exc:
                await self.session.rollback()
                results.append(
                    VisitSyncItemResult(
                        index=index,
                        client_reference=payload.client_reference,
                        status="conflict" if exc.status_code == 409 else "error",
                        error_code=exc.error_code,
                        message=exc.message,
                    )
                )
            except IntegrityError as exc:
                await self.session.rollback()
                logger.warning(
                    "Conflit d'intégrité lors de la synchro",
                    extra={"index": index, "reason": type(exc).__name__},
                )
                results.append(
                    VisitSyncItemResult(
                        index=index,
                        client_reference=payload.client_reference,
                        status="conflict",
                        error_code=DuplicateVisitError.error_code,
                        message=DuplicateVisitError.message,
                    )
                )

        return VisitSyncResponse(
            total=len(payloads),
            created=sum(1 for r in results if r.status == "created"),
            conflicts=sum(1 for r in results if r.status == "conflict"),
            errors=sum(1 for r in results if r.status == "error"),
            results=results,
        )
