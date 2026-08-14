from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AgentNotFoundError,
    AppError,
    ArchivedReferentielError,
    ConflictError,
    DuplicateVisitError,
    ExportFormatUnavailableError,
    PurposeNotFoundError,
    ServiceNotFoundError,
    UnprocessableError,
    VisitAlreadyClosedError,
    VisitCancelledError,
    VisitNotFoundError,
    VisitorAlreadyPresentError,
    VisitorNotFoundError,
)
from app.core.logging import get_logger
from app.models.base import ensure_utc
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
    VisitorRead,
    VisitorSearchResult,
    VisitRead,
    VisitSyncItemResult,
    VisitSyncResponse,
    VisitUpdate,
)
from app.services.audit_service import AuditAction, AuditService, ClientContext, diff
from app.services.export_service import ExportFormat, visits_to_csv
from app.services.setting_service import SettingService

logger = get_logger(__name__)


class VisitService:
    """Logique métier des visites : enregistrement, listing, clôture, synchro offline."""

    def __init__(self, session: AsyncSession, context: ClientContext | None = None) -> None:
        self.session = session
        self.context = context or ClientContext()
        self.visits = VisitRepository(session)
        self.visitors = VisitorRepository(session)
        self.services = ServiceRepository(session)
        self.agents = AgentRepository(session)
        self.purposes = PurposeRepository(session)
        self.audit = AuditService(session)

    async def create_visit(self, payload: VisitCreate, current_user: User) -> Visit:
        """Crée une visite et commit. Les référentiels sont validés avant insertion."""
        visit = await self._build_visit(payload, current_user.id)
        try:
            await self.audit.record(
                AuditAction.VISIT_CREATED,
                entity="visit",
                entity_id=visit.id,
                actor=current_user,
                metadata={
                    "service_id": visit.service_id,
                    "agent_id": visit.agent_id,
                    "client_reference": visit.client_reference,
                    # Trace la reprise d'une fiche connue : la visite a été
                    # enregistrée **sans rescanner** la pièce d'identité.
                    "visiteur_reutilise": payload.visitor_id is not None,
                },
                context=self.context,
            )
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

        service = await self.services.get_by_id(payload.service_id)
        if service is None:
            raise ServiceNotFoundError(details={"service_id": str(payload.service_id)})
        # Un référentiel archivé n'est plus proposé par l'API de lecture : le
        # recevoir ici signale un cache client périmé, pas une saisie valide.
        if service.is_archived:
            raise ArchivedReferentielError(
                "Ce service est archivé.", details={"service_id": str(service.id)}
            )

        agent = await self.agents.get_by_id(payload.agent_id)
        if agent is None:
            raise AgentNotFoundError(details={"agent_id": str(payload.agent_id)})
        if agent.is_archived:
            raise ArchivedReferentielError(
                "Cet agent est archivé.", details={"agent_id": str(agent.id)}
            )
        if agent.service_id != payload.service_id:
            raise ConflictError(
                "L'agent sélectionné n'appartient pas au service indiqué.",
                error_code="AGENT_SERVICE_MISMATCH",
                details={"agent_id": str(agent.id), "service_id": str(payload.service_id)},
            )

        if payload.purpose_id is not None:
            purpose = await self.purposes.get_by_id(payload.purpose_id)
            if purpose is None:
                raise PurposeNotFoundError(details={"purpose_id": str(payload.purpose_id)})
            if purpose.is_archived:
                raise ArchivedReferentielError(
                    "Ce motif est archivé.", details={"purpose_id": str(purpose.id)}
                )

        visitor = await self._resoudre_visiteur(payload)
        await self._refuser_si_deja_present(visitor)

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

    async def _resoudre_visiteur(self, payload: VisitCreate) -> Visitor:
        """Fiche du visiteur : reprise d'une fiche connue, ou créée/mise à jour au scan.

        `VisitCreate` garantit qu'une seule des deux sources est renseignée.
        """
        if payload.visitor_id is None:
            assert payload.visitor is not None
            return await self._upsert_visitor(payload.visitor)

        visitor = await self.visitors.get_by_id(payload.visitor_id)
        if visitor is None:
            raise VisitorNotFoundError(details={"visitor_id": str(payload.visitor_id)})

        if payload.visitor_passage is not None:
            # `exclude_none` : un champ omis ne doit pas effacer une valeur connue.
            modifications = payload.visitor_passage.model_dump(exclude_none=True)
            for champ, valeur in modifications.items():
                setattr(visitor, champ, valeur)
            await self.session.flush()

        return visitor

    async def _refuser_si_deja_present(self, visitor: Visitor) -> None:
        """Une même personne ne peut pas être présente deux fois.

        Le rescan de la pièce freinait naturellement la double saisie ; en reprenant
        une fiche connue, elle ne coûte plus que deux gestes et va se produire. Deux
        visites `PRESENT` pour la même personne fausseraient le compteur des présents
        du dashboard, sans que personne ne les nettoie.

        Le conflit porte la visite ouverte et son heure d'entrée : le client peut
        proposer « clôturer puis réenregistrer » plutôt qu'afficher une impasse.
        """
        ouverte = await self.visits.find_open_visit(visitor.id)
        if ouverte is None:
            return
        raise VisitorAlreadyPresentError(
            details={
                "visitor_id": str(visitor.id),
                "visit_id": str(ouverte.id),
                "checked_in_at": ouverte.checked_in_at,
            }
        )

    async def search_visitors(
        self, terme: str, pagination: PaginationParams, current_user: User
    ) -> Page[VisitorSearchResult]:
        """Retrouve un visiteur déjà venu, pour l'enregistrer sans rescanner sa pièce.

        La route expose de l'identité : sa longueur minimale est imposée côté
        routeur, et le nombre de résultats consultés est tracé dans les logs
        applicatifs. Le journal d'audit n'est pas alimenté ici — une entrée par
        frappe de l'agent le rendrait illisible pour ce qu'il sert vraiment,
        les écritures.
        """
        total = await self.visitors.count_search(terme)
        lignes = await self.visitors.search(
            terme, limit=pagination.page_size, offset=pagination.offset
        )

        logger.info(
            "Recherche de visiteurs",
            extra={"resultats": total, "acteur": str(current_user.id)},
        )

        return Page[VisitorSearchResult](
            items=[
                VisitorSearchResult(
                    **VisitorRead.model_validate(visiteur).model_dump(),
                    derniere_visite_at=derniere,
                    visite_ouverte_id=ouverte_id,
                )
                for visiteur, derniere, ouverte_id in lignes
            ],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )

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
        if visit.statut is VisitStatus.ANNULEE:
            raise VisitCancelledError(details={"visit_id": str(visit_id)})
        if visit.statut is VisitStatus.SORTI:
            raise VisitAlreadyClosedError(
                details={"visit_id": str(visit_id), "checked_out_at": visit.checked_out_at}
            )

        visit.statut = VisitStatus.SORTI
        visit.checked_out_at = datetime.now(UTC)
        visit.checked_out_by = current_user.id
        await self.audit.record(
            AuditAction.VISIT_CHECKOUT,
            entity="visit",
            entity_id=visit.id,
            actor=current_user,
            metadata={"checked_out_at": visit.checked_out_at},
            context=self.context,
        )
        await self.session.commit()

        refreshed = await self.visits.get_by_id(visit.id)
        assert refreshed is not None
        return refreshed

    # --- Administration (rôle ADMIN) -----------------------------------------

    async def update_visit(
        self, visit_id: uuid.UUID, payload: VisitUpdate, current_user: User
    ) -> Visit:
        """Corrige une visite mal saisie.

        Le motif est obligatoire et part au journal avec le diff avant/après :
        c'est ce qui rend la correction défendable lors d'un audit. La visite
        elle-même ne porte pas ce motif — elle n'est pas censée raconter son
        histoire, le journal s'en charge.
        """
        visit = await self.get_visit(visit_id)
        if visit.statut is VisitStatus.ANNULEE:
            raise VisitCancelledError(details={"visit_id": str(visit_id)})

        modifications = payload.model_dump(exclude={"reason"}, exclude_unset=True)
        await self._verifier_references(visit, modifications)

        champs = [
            "service_id",
            "agent_id",
            "purpose_id",
            "motif_libre",
            "badge_number",
            "checked_in_at",
            "checked_out_at",
        ]
        avant = {champ: getattr(visit, champ) for champ in champs}
        for champ, valeur in modifications.items():
            setattr(visit, champ, valeur)
        apres = {champ: getattr(visit, champ) for champ in champs}

        if visit.checked_out_at is not None and ensure_utc(visit.checked_out_at) < ensure_utc(
            visit.checked_in_at
        ):
            raise UnprocessableError(
                "La sortie ne peut pas précéder l'entrée.",
                error_code="INVALID_VISIT_INTERVAL",
                details={
                    "checked_in_at": visit.checked_in_at,
                    "checked_out_at": visit.checked_out_at,
                },
            )
        # Corriger l'horodatage de sortie d'une visite encore ouverte revient à la
        # clôturer : le statut doit suivre, sinon la visite resterait « présente »
        # avec une date de sortie renseignée.
        if visit.checked_out_at is not None and visit.statut is VisitStatus.PRESENT:
            visit.statut = VisitStatus.SORTI
            visit.checked_out_by = current_user.id

        await self.audit.record(
            AuditAction.VISIT_UPDATED,
            entity="visit",
            entity_id=visit.id,
            actor=current_user,
            metadata={"reason": payload.reason, "changements": diff(avant, apres)},
            context=self.context,
        )
        await self.session.commit()

        refreshed = await self.visits.get_by_id(visit.id)
        assert refreshed is not None
        return refreshed

    async def cancel_visit(self, visit_id: uuid.UUID, reason: str, current_user: User) -> Visit:
        """Annulation logique. La visite reste en base, avec son motif d'annulation."""
        visit = await self.get_visit(visit_id)
        if visit.statut is VisitStatus.ANNULEE:
            raise VisitCancelledError(details={"visit_id": str(visit_id)})

        visit.statut = VisitStatus.ANNULEE
        visit.cancelled_at = datetime.now(UTC)
        visit.cancelled_by = current_user.id
        visit.cancellation_reason = reason

        await self.audit.record(
            AuditAction.VISIT_CANCELLED,
            entity="visit",
            entity_id=visit.id,
            actor=current_user,
            metadata={"reason": reason},
            context=self.context,
        )
        await self.session.commit()

        refreshed = await self.visits.get_by_id(visit.id)
        assert refreshed is not None
        return refreshed

    async def export_visits(
        self, filters: VisitFilters, fmt: ExportFormat, current_user: User
    ) -> tuple[bytes, str]:
        """Exporte le registre filtré. Retourne `(contenu, type MIME)`."""
        if fmt != "csv":
            raise ExportFormatUnavailableError(
                "Seul l'export CSV est disponible pour le moment.",
                details={"format": fmt, "formats_disponibles": ["csv"]},
            )

        parametres = await SettingService(self.session).get_settings()
        plafond = parametres.visits_export_max_rows

        # `plafond + 1` : si la requête ramène une ligne de plus que le plafond,
        # c'est qu'il y en a davantage. On refuse alors, plutôt que de livrer un
        # export tronqué que personne ne remarquerait.
        visits = await self.visits.list_for_export(filters, limit=plafond + 1)
        if len(visits) > plafond:
            raise UnprocessableError(
                "L'export dépasse le nombre de lignes autorisé : affinez les filtres.",
                error_code="EXPORT_TOO_LARGE",
                details={"max_rows": plafond},
            )

        await self.audit.record(
            AuditAction.VISIT_EXPORTED,
            entity="visit",
            actor=current_user,
            metadata={
                "format": fmt,
                "lignes": len(visits),
                "filtres": filters.model_dump(exclude_none=True),
            },
            context=self.context,
        )
        await self.session.commit()
        return visits_to_csv(visits), "text/csv; charset=utf-8"

    async def _verifier_references(self, visit: Visit, modifications: dict[str, Any]) -> None:
        """Contrôle les référentiels d'une correction, y compris leur cohérence croisée.

        Les valeurs proviennent de `VisitUpdate.model_dump()` : Pydantic a déjà
        validé qu'un `service_id` fourni est bien un UUID.
        """
        service_id: uuid.UUID = modifications.get("service_id") or visit.service_id
        agent_id: uuid.UUID = modifications.get("agent_id") or visit.agent_id

        if "service_id" in modifications and not await self.services.exists(service_id):
            raise ServiceNotFoundError(details={"service_id": str(service_id)})

        if "agent_id" in modifications or "service_id" in modifications:
            agent = await self.agents.get_by_id(agent_id)
            if agent is None:
                raise AgentNotFoundError(details={"agent_id": str(agent_id)})
            if agent.service_id != service_id:
                raise ConflictError(
                    "L'agent sélectionné n'appartient pas au service indiqué.",
                    error_code="AGENT_SERVICE_MISMATCH",
                    details={"agent_id": str(agent_id), "service_id": str(service_id)},
                )

        purpose_id: uuid.UUID | None = modifications.get("purpose_id")
        if purpose_id is not None and not await self.purposes.exists(purpose_id):
            raise PurposeNotFoundError(details={"purpose_id": str(purpose_id)})

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
