"""Synchronisation hors-ligne des visites (ADR-025)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.errors import (
    AppError,
    BadRequestError,
    CrossOrganizationError,
    DuplicateVisitError,
    UnprocessableError,
    VisitCancelledError,
    VisitNotFoundError,
)
from app.core.logging import get_logger
from app.models.base import ensure_utc
from app.models.enums import CheckoutOrigin, ForgottenDocumentStatus, VisitStatus
from app.models.user import User
from app.models.visit import Visit
from app.repositories.forgotten_document_repository import ForgottenDocumentRepository
from app.schemas.visit import (
    VisitCreate,
    VisitSyncCheckout,
    VisitSyncCheckoutResult,
    VisitSyncEntry,
    VisitSyncItemResult,
    VisitSyncRequest,
    VisitSyncResponse,
)
from app.services.audit_service import AuditAction
from app.services.visit_service import VisitService

logger = get_logger(__name__)


class VisitSyncService(VisitService):
    """Rejoue les entrées et sorties saisies sans réseau, une opération à la fois."""

    async def sync_visits(
        self,
        request: VisitSyncRequest,
        current_user: User,
        *,
        organisation: uuid.UUID | None,
    ) -> VisitSyncResponse:
        """Applique un lot d'entrées et de sorties saisies hors-ligne.

        `organisation` est celle de l'appelant (`None` pour l'ADMIN de
        plateforme) : chaque opération y est cantonnée, comme en ligne.

        Les opérations sont rejouées **dans l'ordre chronologique** de leur
        horodatage, entrées et sorties mêlées. C'est ce qui permet d'envoyer en
        un seul lot un aller-retour saisi pendant une coupure : la sortie de la
        première visite précède la seconde entrée, qui sinon serait refusée en
        `VISITOR_ALREADY_PRESENT`. À horodatage égal, l'entrée passe d'abord.

        Chaque opération est commitée seule : une ligne en échec ne coûte pas les
        autres, et un lot renvoyé après une coupure est rejoué sans doublon — la
        `client_reference` d'une entrée déjà enregistrée répond `already_synced`,
        une sortie déjà enregistrée `already_closed`.
        """
        # Résolu une seule fois : l'objet `User` serait expiré par le premier rollback.
        user_id = current_user.id

        operations = [
            (ensure_utc(entree.checked_in_at), 0, index)
            for index, entree in enumerate(request.visits)
        ] + [
            (ensure_utc(sortie.checked_out_at), 1, index)
            for index, sortie in enumerate(request.checkouts)
        ]
        operations.sort()

        entrees: dict[int, VisitSyncItemResult] = {}
        sorties: dict[int, VisitSyncCheckoutResult] = {}
        for _, nature, index in operations:
            if nature == 0:
                entrees[index] = await self._synchroniser_entree(
                    index, request.visits[index], user_id, organisation
                )
            else:
                sorties[index] = await self._synchroniser_sortie(
                    index, request.checkouts[index], user_id, organisation
                )

        return self._bilan_synchro(
            [entrees[i] for i in range(len(request.visits))],
            [sorties[i] for i in range(len(request.checkouts))],
        )

    async def _synchroniser_entree(
        self,
        index: int,
        entree: VisitSyncEntry,
        user_id: uuid.UUID,
        organisation: uuid.UUID | None,
    ) -> VisitSyncItemResult:
        reference = entree.client_reference
        try:
            payload = self._cantonner(entree, organisation)
            visit = await self._creer_avec_une_reprise(payload, user_id)
            return VisitSyncItemResult(
                index=index, client_reference=reference, status="created", visit_id=visit.id
            )
        except DuplicateVisitError as exc:
            # La référence est déjà au registre : c'est la même entrée, envoyée
            # une seconde fois parce que la réponse du premier envoi s'est perdue.
            # Un succès, avec l'identifiant qui permet au client de se recaler.
            await self.session.rollback()
            existante = (exc.details or {}).get("visit_id")
            return VisitSyncItemResult(
                index=index,
                client_reference=reference,
                status="already_synced" if existante else "conflict",
                visit_id=existante,
                error_code=None if existante else exc.error_code,
                message=None if existante else exc.message,
            )
        except AppError as exc:
            await self.session.rollback()
            return VisitSyncItemResult(
                index=index,
                client_reference=reference,
                status="conflict" if exc.status_code == 409 else "error",
                error_code=exc.error_code,
                message=exc.message,
                details=exc.details,
            )
        except IntegrityError as exc:
            await self.session.rollback()
            logger.warning(
                "Conflit d'intégrité lors de la synchro",
                extra={"index": index, "reason": type(exc).__name__},
            )
            return VisitSyncItemResult(
                index=index,
                client_reference=reference,
                status="conflict",
                error_code=DuplicateVisitError.error_code,
                message=DuplicateVisitError.message,
            )

    @staticmethod
    def _cantonner(entree: VisitSyncEntry, organisation: uuid.UUID | None) -> VisitSyncEntry:
        """Impose l'organisation de l'appelant à une entrée du lot.

        Sans cela, une entrée sans `organization_id` prenait celle de son service :
        un `service_id` d'une autre structure suffisait à écrire dans son
        registre. L'ADMIN de plateforme, sans organisation propre, garde la règle
        d'avant — celle du service.
        """
        if organisation is None:
            return entree
        if entree.organization_id not in (None, organisation):
            raise CrossOrganizationError()
        return entree.model_copy(update={"organization_id": organisation})

    async def _creer_avec_une_reprise(self, payload: VisitCreate, user_id: uuid.UUID) -> Visit:
        """Crée et commit une visite du lot, avec une reprise sur conflit d'unicité.

        Deux tablettes qui synchronisent la même personne au même instant se
        heurtent à l'unicité de la pièce : la seconde reprend alors la fiche
        créée par la première, au lieu de perdre sa visite.
        """
        try:
            return await self._creer_et_tracer(payload, user_id)
        except IntegrityError:
            await self.session.rollback()
        return await self._creer_et_tracer(payload, user_id)

    async def _creer_et_tracer(self, payload: VisitCreate, user_id: uuid.UUID) -> Visit:
        visit = await self._build_visit(payload, user_id)
        visit.recorded_offline = True
        await self.audit.record(
            AuditAction.VISIT_CREATED,
            entity="visit",
            entity_id=visit.id,
            actor=await self._auteur(user_id),
            organization_id=visit.organization_id,
            metadata={
                "service_id": visit.service_id,
                "agent_id": visit.agent_id,
                "client_reference": visit.client_reference,
                "visiteur_reutilise": payload.visitor_id is not None,
                "source": "sync",
            },
            context=self.context,
        )
        await self.session.commit()
        return visit

    async def _auteur(self, user_id: uuid.UUID) -> User | None:
        """L'auteur, relu : un rollback précédent du lot a expiré l'instance."""
        return await self.session.get(User, user_id, populate_existing=True)

    async def _synchroniser_sortie(
        self,
        index: int,
        sortie: VisitSyncCheckout,
        user_id: uuid.UUID,
        organisation: uuid.UUID | None,
    ) -> VisitSyncCheckoutResult:
        try:
            return await self._cloturer_hors_ligne(index, sortie, user_id, organisation)
        except AppError as exc:
            await self.session.rollback()
            return VisitSyncCheckoutResult(
                index=index,
                visit_id=sortie.visit_id,
                client_reference=sortie.client_reference,
                status="conflict" if exc.status_code == 409 else "error",
                error_code=exc.error_code,
                message=exc.message,
                details=exc.details,
            )

    async def _cloturer_hors_ligne(
        self,
        index: int,
        sortie: VisitSyncCheckout,
        user_id: uuid.UUID,
        organisation: uuid.UUID | None,
    ) -> VisitSyncCheckoutResult:
        """Enregistre une sortie à l'heure où elle a eu lieu, pas à l'heure du réseau.

        Une visite déjà clôturée **automatiquement** est corrigée : l'heure saisie
        par l'agent est la vraie, celle de la tâche de 17 h n'était qu'un repli.
        La fiche de pièce potentiellement oubliée qu'elle avait ouverte est
        résolue — la sortie enregistrée au poste prouve que la pièce a été rendue.
        """
        visit = await self._visite_a_cloturer(sortie, organisation)
        moment = ensure_utc(sortie.checked_out_at)
        self._refuser_horodatage_futur(moment, "checked_out_at")

        if visit.statut is VisitStatus.ANNULEE:
            raise VisitCancelledError(details={"visit_id": str(visit.id)})
        if moment < ensure_utc(visit.checked_in_at):
            raise UnprocessableError(
                "La sortie ne peut pas précéder l'entrée.",
                error_code="INVALID_VISIT_INTERVAL",
                details={
                    "visit_id": str(visit.id),
                    "checked_in_at": visit.checked_in_at,
                    "checked_out_at": moment,
                },
            )

        resultat: dict[str, Any] = {
            "index": index,
            "visit_id": visit.id,
            "client_reference": sortie.client_reference or visit.client_reference,
        }
        if visit.statut is VisitStatus.SORTI and visit.checkout_origin is not (
            CheckoutOrigin.AUTOMATIC
        ):
            return VisitSyncCheckoutResult(
                **resultat,
                status="already_closed",
                checked_out_at=ensure_utc(visit.checked_out_at) if visit.checked_out_at else None,
            )

        correction = visit.statut is VisitStatus.SORTI
        sortie_automatique = visit.checked_out_at
        visit.statut = VisitStatus.SORTI
        visit.checked_out_at = moment
        visit.checked_out_by = user_id
        visit.checkout_origin = CheckoutOrigin.MANUAL

        auteur = await self._auteur(user_id)
        await self.audit.record(
            AuditAction.VISIT_CHECKOUT,
            entity="visit",
            entity_id=visit.id,
            actor=auteur,
            organization_id=visit.organization_id,
            metadata={
                "checked_out_at": moment,
                "source": "sync",
                **(
                    {"cloture_automatique_corrigee": sortie_automatique}
                    if correction
                    else {}
                ),
            },
            context=self.context,
        )
        if correction:
            await self._resoudre_piece_oubliee(visit, user_id, auteur, moment)
        await self.session.commit()

        return VisitSyncCheckoutResult(
            **resultat,
            status="closed",
            checked_out_at=moment,
            details={"cloture_automatique_corrigee": True} if correction else None,
        )

    async def _visite_a_cloturer(
        self, sortie: VisitSyncCheckout, organisation: uuid.UUID | None
    ) -> Visit:
        if sortie.visit_id is not None:
            visit = await self.visits.get_by_id(sortie.visit_id)
            if visit is None:
                raise VisitNotFoundError(details={"visit_id": str(sortie.visit_id)})
            if organisation is not None and visit.organization_id != organisation:
                raise CrossOrganizationError("Accès limité aux visites de votre organisation.")
            return visit

        if sortie.client_reference is None:  # invariant : un `assert` disparaît sous `python -O`
            raise RuntimeError("Invariant violé : sortie.client_reference est None.")
        if organisation is None:
            # La référence n'est unique qu'au sein d'une organisation.
            raise BadRequestError(
                "Un compte de plateforme désigne la visite par `visit_id`.",
                details={"client_reference": sortie.client_reference},
            )
        visit = await self.visits.get_by_client_reference(sortie.client_reference, organisation)
        if visit is None:
            raise VisitNotFoundError(details={"client_reference": sortie.client_reference})
        return visit

    async def _resoudre_piece_oubliee(
        self, visit: Visit, user_id: uuid.UUID, auteur: User | None, moment: datetime
    ) -> None:
        fiche = await ForgottenDocumentRepository(self.session).get_by_visit_id(visit.id)
        if fiche is None or fiche.status is ForgottenDocumentStatus.RESOLVED:
            return
        fiche.status = ForgottenDocumentStatus.RESOLVED
        fiche.resolved_at = datetime.now(UTC)
        fiche.resolved_by = user_id
        fiche.resolution_note = (
            "Sortie enregistrée hors-ligne au poste de contrôle "
            f"({moment.isoformat(timespec='minutes')}) : pièce restituée."
        )
        await self.audit.record(
            AuditAction.FORGOTTEN_DOC_RESOLVED,
            entity="forgotten_document_case",
            entity_id=fiche.id,
            actor=auteur,
            organization_id=visit.organization_id,
            metadata={"visit_id": str(visit.id), "source": "sync"},
            context=self.context,
        )

    @staticmethod
    def _bilan_synchro(
        entrees: list[VisitSyncItemResult], sorties: list[VisitSyncCheckoutResult]
    ) -> VisitSyncResponse:
        def compte(resultats: list[Any], statut: str) -> int:
            return sum(1 for r in resultats if r.status == statut)

        return VisitSyncResponse(
            server_time=datetime.now(UTC),
            total=len(entrees),
            created=compte(entrees, "created"),
            already_synced=compte(entrees, "already_synced"),
            conflicts=compte(entrees, "conflict"),
            errors=compte(entrees, "error"),
            results=entrees,
            checkout_total=len(sorties),
            checkout_closed=compte(sorties, "closed"),
            checkout_already_closed=compte(sorties, "already_closed"),
            checkout_conflicts=compte(sorties, "conflict"),
            checkout_errors=compte(sorties, "error"),
            checkout_results=sorties,
        )
