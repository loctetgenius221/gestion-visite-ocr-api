from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    AgentNotFoundError,
    ArchivedReferentielError,
    ConflictError,
    CrossOrganizationError,
    DuplicateVisitError,
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
from app.models.enums import CheckoutOrigin, UserRole, VisitStatus
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
)
from app.services.audit_service import AuditAction, AuditService, ClientContext

logger = get_logger(__name__)

# Avance tolérée sur l'horloge du serveur pour une heure envoyée par un client.
TOLERANCE_HORLOGE = timedelta(minutes=5)


class VisitService:
    """Logique métier des visites : enregistrement, recherche, lecture, clôture.

    L'administration (`VisitAdminService`) et la synchronisation hors-ligne
    (`VisitSyncService`) en héritent : elles réutilisent l'enregistrement et la
    lecture cloisonnée, sans les dupliquer.
    """

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
        if refreshed is None:  # invariant : un `assert` disparaît sous `python -O`
            raise RuntimeError("Invariant violé : refreshed est None.")
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
            if payload.visitor is None:  # invariant : un `assert` disparaît sous `python -O`
                raise RuntimeError("Invariant violé : payload.visitor est None.")
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
        if refreshed is None:  # invariant : un `assert` disparaît sous `python -O`
            raise RuntimeError("Invariant violé : refreshed est None.")
        return refreshed

    # --- Garde-fous d'horodatage (en ligne et en synchro) -----------------------

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
