from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    AgentNotFoundError,
    AppError,
    ArchivedReferentielError,
    BadRequestError,
    ConflictError,
    CrossOrganizationError,
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
from app.core.storage_urls import appartient_a_une_autre_organisation
from app.models.base import ensure_utc
from app.models.enums import CheckoutOrigin, ForgottenDocumentStatus, UserRole, VisitStatus
from app.models.user import User
from app.models.visit import Visit
from app.models.visitor import Visitor
from app.repositories.forgotten_document_repository import ForgottenDocumentRepository
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
    VisitSyncCheckout,
    VisitSyncCheckoutResult,
    VisitSyncEntry,
    VisitSyncItemResult,
    VisitSyncRequest,
    VisitSyncResponse,
    VisitUpdate,
)
from app.services.audit_service import AuditAction, AuditService, ClientContext, diff
from app.services.export_service import ExportFormat, visits_to_csv
from app.services.setting_service import SettingService

logger = get_logger(__name__)

# Avance tolérée sur l'horloge du serveur pour une heure envoyée par un client.
TOLERANCE_HORLOGE = timedelta(minutes=5)


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
        """Crée une visite et commit. Les référentiels sont validés avant insertion.

        Une `IntegrityError` signale une course : deux enregistrements simultanés
        de la même personne (unicité de la pièce, ADR-010) ou de la même
        `client_reference`. La seconde tentative relit l'état du gagnant — la
        fiche créée entre-temps est reprise, une référence déjà enregistrée
        répond `DUPLICATE_VISIT` — au lieu de renvoyer une erreur à l'agent.
        """
        for tentative in (1, 2):
            try:
                visit = await self._build_visit(payload, current_user.id)
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
                break
            except IntegrityError as exc:
                await self.session.rollback()
                if tentative == 2:
                    raise DuplicateVisitError(
                        details={"client_reference": payload.client_reference}
                    ) from exc
                # Le rollback a expiré toutes les instances de la session : la
                # relecture doit être explicite, une lecture paresseuse étant
                # interdite en contexte async.
                await self.session.refresh(current_user)

        refreshed = await self.visits.get_by_id(visit.id)
        assert refreshed is not None
        return refreshed

    async def _build_visit(self, payload: VisitCreate, user_id: uuid.UUID) -> Visit:
        """Valide les références et prépare la visite en session (sans commit).

        On reçoit l'`user_id` et non l'objet `User` : un rollback expire toutes les
        instances de la session, et relire un attribut expiré déclencherait une IO
        synchrone interdite ici (cas de la synchro batch, qui rollback entre items).

        Toute référence doit appartenir à l'organisation de la visite : service,
        motif, fichiers déposés, visiteur repris (ADR-023). Le router impose déjà
        l'organisation de l'appelant ; sans ce contrôle, un identifiant de service
        d'une autre structure suffisait à écrire dans son registre.
        """
        if payload.checked_in_at is not None:
            self._refuser_horodatage_futur(payload.checked_in_at, "checked_in_at")
            self._refuser_horodatage_trop_ancien(payload.checked_in_at)
        service = await self.services.get_by_id(payload.service_id)
        if service is None:
            raise ServiceNotFoundError(details={"service_id": str(payload.service_id)})
        organisation = payload.organization_id or service.organization_id
        if service.organization_id != organisation:
            raise CrossOrganizationError(
                "Ce service appartient à une autre organisation.",
                details={"service_id": str(service.id)},
            )
        # Un référentiel archivé n'est plus proposé par l'API de lecture : le
        # recevoir ici signale un cache client périmé, pas une saisie valide.
        if service.is_archived:
            raise ArchivedReferentielError(
                "Ce service est archivé.", details={"service_id": str(service.id)}
            )

        if payload.client_reference:
            existing = await self.visits.get_by_client_reference(
                payload.client_reference, organisation
            )
            if existing is not None:
                raise DuplicateVisitError(
                    details={
                        "client_reference": payload.client_reference,
                        "visit_id": str(existing.id),
                    }
                )

        # Facultative : un dépôt ou un retrait s'adresse au service, pas à une
        # personne (ADR-019). Fournie, elle reste contrôlée comme avant.
        if payload.agent_id is not None:
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
            # Un motif commun (`organization_id` nul) est proposé à toutes les
            # structures ; un motif propre n'appartient qu'à la sienne.
            if purpose.organization_id not in (None, organisation):
                raise CrossOrganizationError(
                    "Ce motif appartient à une autre organisation.",
                    details={"purpose_id": str(purpose.id)},
                )
            if purpose.is_archived:
                raise ArchivedReferentielError(
                    "Ce motif est archivé.", details={"purpose_id": str(purpose.id)}
                )

        self._refuser_fichiers_etrangers(payload, organisation)
        visitor = await self._resoudre_visiteur(payload, organisation)
        await self._refuser_si_deja_present(visitor, organisation)

        visit = Visit(
            organization_id=organisation,
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

    @staticmethod
    def _refuser_fichiers_etrangers(payload: VisitCreate, organisation: uuid.UUID) -> None:
        """Refuse un fichier rangé dans le dossier d'une autre organisation.

        Le chemin n'est pas renvoyé dans l'erreur : il désigne un fichier d'autrui.
        """
        chemins = [payload.signature_url]
        if payload.visitor is not None:
            chemins += [
                payload.visitor.document_recto_url,
                payload.visitor.document_verso_url,
                payload.visitor.mrz_image_url,
            ]
        if any(c and appartient_a_une_autre_organisation(c, organisation) for c in chemins):
            raise CrossOrganizationError(
                "Un fichier référencé a été déposé par une autre organisation."
            )

    async def _resoudre_visiteur(self, payload: VisitCreate, organisation: uuid.UUID) -> Visitor:
        """Fiche du visiteur : reprise d'une fiche connue, ou créée/mise à jour au scan.

        `VisitCreate` garantit qu'une seule des deux sources est renseignée.

        Une fiche ne se reprend par son identifiant que dans l'organisation qui la
        tient (ADR-026). Une fiche d'autrui répond comme une fiche inexistante,
        pour ne pas confirmer qu'elle existe ailleurs.
        """
        if payload.visitor_id is None:
            assert payload.visitor is not None
            return await self._upsert_visitor(payload.visitor, organisation)

        visitor = await self.visitors.get_by_id(payload.visitor_id)
        if visitor is None or visitor.organization_id != organisation:
            raise VisitorNotFoundError(details={"visitor_id": str(payload.visitor_id)})

        if payload.visitor_passage is not None:
            # `exclude_none` : un champ omis ne doit pas effacer une valeur connue.
            modifications = payload.visitor_passage.model_dump(exclude_none=True)
            for champ, valeur in modifications.items():
                setattr(visitor, champ, valeur)
            await self.session.flush()

        return visitor

    async def _refuser_si_deja_present(self, visitor: Visitor, organisation: uuid.UUID) -> None:
        """Une même personne ne peut pas être présente deux fois **dans un même registre**.

        Le rescan de la pièce freinait naturellement la double saisie ; en reprenant
        une fiche connue, elle ne coûte plus que deux gestes et va se produire. Deux
        visites `PRESENT` pour la même personne fausseraient le compteur des présents
        du dashboard, sans que personne ne les nettoie.

        Le conflit porte la visite ouverte et son heure d'entrée : le client peut
        proposer « clôturer puis réenregistrer » plutôt qu'afficher une impasse.
        """
        ouverte = await self.visits.find_open_visit(visitor.id, organisation)
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
        self,
        terme: str,
        pagination: PaginationParams,
        current_user: User,
        *,
        organization_id: uuid.UUID | None = None,
    ) -> Page[VisitorSearchResult]:
        """Retrouve un visiteur déjà venu, pour l'enregistrer sans rescanner sa pièce.

        La route expose de l'identité : sa longueur minimale est imposée côté
        routeur, le périmètre d'organisation est imposé par le routeur également,
        et le nombre de résultats consultés est tracé dans les logs applicatifs.
        Le journal d'audit n'est pas alimenté ici — une entrée par frappe de
        l'agent le rendrait illisible pour ce qu'il sert vraiment, les écritures.
        """
        total = await self.visitors.count_search(terme, organization_id=organization_id)
        lignes = await self.visitors.search(
            terme,
            limit=pagination.page_size,
            offset=pagination.offset,
            organization_id=organization_id,
        )

        logger.info(
            "Recherche de visiteurs",
            extra={
                "resultats": total,
                "acteur": str(current_user.id),
                "organisation": str(organization_id) if organization_id else "toutes",
            },
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

    async def _upsert_visitor(self, data: VisitorInput, organisation: uuid.UUID) -> Visitor:
        """Réutilise le visiteur existant (même document) et rafraîchit ses coordonnées.

        Un même visiteur revient régulièrement au poste d'accueil : dupliquer sa fiche
        à chaque passage fausserait la recherche et l'historique.

        La recherche et la mise à jour ne portent que sur les fiches de
        l'organisation (ADR-026). Une fiche commune à tout le parc renvoyait à un
        ministère le NIN et les photos saisis par un autre, et laissait chacun
        réécrire l'identité figurant dans le registre des autres (SEC-001).
        """
        existing = await self.visitors.get_by_document(
            data.type_document, data.numero_document, organisation
        )
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

        return await self.visitors.add(Visitor(organization_id=organisation, **payload))

    async def get_visit(self, visit_id: uuid.UUID, current_user: User | None = None) -> Visit:
        visit = await self.visits.get_by_id(visit_id)
        if visit is None:
            raise VisitNotFoundError(details={"visit_id": str(visit_id)})
        # Le rôle décide, pas la nullité du rattachement : `organization_id is
        # None` signifie « administrateur de plateforme » pour un ADMIN, mais
        # aurait signifié « accès à tout » pour n'importe quel compte mal
        # rattaché. Toutes les écritures sur une visite passent par ici.
        if (
            current_user is not None
            and current_user.role is not UserRole.ADMIN
            and visit.organization_id != current_user.organization_id
        ):
            raise CrossOrganizationError("Accès limité aux visites de votre organisation.")
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
        visit = await self.get_visit(visit_id, current_user)
        if visit.statut is VisitStatus.ANNULEE:
            raise VisitCancelledError(details={"visit_id": str(visit_id)})
        if visit.statut is VisitStatus.SORTI:
            raise VisitAlreadyClosedError(
                details={"visit_id": str(visit_id), "checked_out_at": visit.checked_out_at}
            )

        visit.statut = VisitStatus.SORTI
        visit.checked_out_at = datetime.now(UTC)
        visit.checked_out_by = current_user.id
        visit.checkout_origin = CheckoutOrigin.MANUAL
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
        visit = await self.get_visit(visit_id, current_user)
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
        visit = await self.get_visit(visit_id, current_user)
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

    async def delete_visit(self, visit_id: uuid.UUID, current_user: User) -> None:
        """Suppression **définitive** d'une visite.

        À distinguer de `cancel_visit`, qui est la voie normale : une visite
        annulée reste au registre, motif à l'appui, et se relit des années après.
        Ici la ligne disparaît — réservé aux saisies qui n'auraient jamais dû
        exister (test, doublon manifeste), pas à la correction d'une visite réelle.

        Le journal devient alors la **seule** trace restante : l'entrée
        `visit.deleted` emporte donc un instantané complet de ce qui est détruit,
        et non le seul identifiant. Sans lui, la question « que contenait cette
        visite ? » n'aurait plus de réponse nulle part, ce qui retirerait au
        registre la vérifiabilité qui en fait un registre.

        Le visiteur, lui, n'est pas supprimé : sa fiche sert aux autres passages,
        et ses photos de pièce relèvent de la purge (ADR-018).
        """
        visit = await self.get_visit(visit_id, current_user)

        await self.audit.record(
            AuditAction.VISIT_DELETED,
            entity="visit",
            entity_id=visit.id,
            actor=current_user,
            metadata={"visite": self._instantane(visit)},
            context=self.context,
        )
        await self.visits.delete(visit)
        await self.session.commit()

        logger.info(
            "Visite supprimée",
            extra={"visit_id": str(visit_id), "acteur": str(current_user.id)},
        )

    @staticmethod
    def _instantane(visit: Visit) -> dict[str, Any]:
        """Contenu d'une visite, figé pour le journal avant sa suppression.

        Le visiteur est nommé en clair : relire l'entrée des mois plus tard ne doit
        pas dépendre d'un `visitor_id` dont la fiche a pu être purgée depuis.
        """
        return {
            "visitor_id": visit.visitor_id,
            "visiteur": f"{visit.visitor.prenom} {visit.visitor.nom}",
            "numero_document": visit.visitor.numero_document,
            "service_id": visit.service_id,
            "agent_id": visit.agent_id,
            "purpose_id": visit.purpose_id,
            "motif_libre": visit.motif_libre,
            "badge_number": visit.badge_number,
            "statut": visit.statut,
            "checked_in_at": visit.checked_in_at,
            "checked_out_at": visit.checked_out_at,
            "checked_in_by": visit.checked_in_by,
            "checked_out_by": visit.checked_out_by,
            "cancelled_at": visit.cancelled_at,
            "cancellation_reason": visit.cancellation_reason,
            "client_reference": visit.client_reference,
        }

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

        Le repli passe par `get(clé, défaut)` et non par un `or` : depuis que la
        personne rencontrée est facultative (ADR-019), `{"agent_id": null}` veut
        dire « retire-la ». Un `or` confondrait cet effacement volontaire avec une
        absence de modification, et retomberait sur l'agent actuel.
        """
        service_id: uuid.UUID = modifications.get("service_id", visit.service_id)
        agent_id: uuid.UUID | None = modifications.get("agent_id", visit.agent_id)

        if "service_id" in modifications:
            service = await self.services.get_by_id(service_id)
            if service is None:
                raise ServiceNotFoundError(details={"service_id": str(service_id)})
            # Une correction ne fait pas changer une visite de registre (ADR-023).
            if service.organization_id != visit.organization_id:
                raise CrossOrganizationError(
                    "Ce service appartient à une autre organisation.",
                    details={"service_id": str(service_id)},
                )

        # Sans personne rencontrée, il n'y a plus de cohérence agent/service à
        # vérifier — y compris quand c'est le service qui change.
        if agent_id is not None and ("agent_id" in modifications or "service_id" in modifications):
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
        if purpose_id is not None:
            purpose = await self.purposes.get_by_id(purpose_id)
            if purpose is None:
                raise PurposeNotFoundError(details={"purpose_id": str(purpose_id)})
            if purpose.organization_id not in (None, visit.organization_id):
                raise CrossOrganizationError(
                    "Ce motif appartient à une autre organisation.",
                    details={"purpose_id": str(purpose_id)},
                )

    # --- Synchronisation hors-ligne (ADR-025) ----------------------------------

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

        resultat = {
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

        assert sortie.client_reference is not None
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
    def _refuser_horodatage_futur(moment: datetime, champ: str) -> None:
        """Refuse une heure postérieure à celle du serveur, à la tolérance près.

        Une tablette dont l'horloge avance inscrirait des visites dans le futur :
        elles échapperaient à la clôture automatique et fausseraient les
        statistiques du jour. La tolérance absorbe la dérive ordinaire d'une
        horloge synchronisée par le réseau mobile.
        """
        maintenant = datetime.now(UTC)
        if ensure_utc(moment) > maintenant + TOLERANCE_HORLOGE:
            raise UnprocessableError(
                "L'heure envoyée est dans le futur : vérifiez l'horloge de l'appareil.",
                error_code="TIMESTAMP_IN_FUTURE",
                details={
                    "champ": champ,
                    "valeur": moment,
                    "heure_serveur": maintenant,
                    "tolerance_secondes": int(TOLERANCE_HORLOGE.total_seconds()),
                },
            )

    @staticmethod
    def _refuser_horodatage_trop_ancien(moment: datetime) -> None:
        """Refuse une entrée antérieure à `VISIT_MAX_BACKDATE_DAYS` (audit SEC-009).

        Sans borne, un agent inscrivait une visite à n'importe quelle date passée,
        l'an dernier par exemple, sans que rien ne la distingue d'un passage réel.
        La limite couvre la plus longue coupure réseau prévue ; une date plus
        ancienne se corrige par l'administration des visites, qui la trace.
        """
        maintenant = datetime.now(UTC)
        limite = maintenant - timedelta(days=settings.VISIT_MAX_BACKDATE_DAYS)
        if ensure_utc(moment) < limite:
            raise UnprocessableError(
                "L'heure d'entrée est trop ancienne : faites corriger la visite par un "
                "administrateur.",
                error_code="TIMESTAMP_TOO_OLD",
                details={
                    "champ": "checked_in_at",
                    "valeur": moment,
                    "heure_serveur": maintenant,
                    "max_jours": settings.VISIT_MAX_BACKDATE_DAYS,
                },
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
