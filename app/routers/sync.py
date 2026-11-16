"""Préparation du travail hors-ligne de l'app mobile (ADR-025).

Deux lectures, à rafraîchir à chaque retour du réseau, avant d'envoyer la file
d'attente par `POST /visits/sync` :

- `GET /sync/referentiels` : services, agents, motifs et réglages, avec un
  `ETag` qui réduit le rafraîchissement courant à un `304` sans corps ;
- `GET /sync/presents` : les visites ouvertes, pour enregistrer une sortie sans
  réseau, et savoir qu'une personne est déjà là avant de l'enregistrer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.core.deps import OrganizationFilter, SessionDep
from app.core.errors import OrganizationNotFoundError, OrganizationRequiredError
from app.core.routing import StrictQueryParamsRoute
from app.models.enums import VisitStatus
from app.models.organization import Organization
from app.repositories.referentiel_repository import (
    AgentRepository,
    PurposeRepository,
    ServiceRepository,
)
from app.repositories.visit_repository import VisitRepository
from app.schemas.organization import OrganizationThemeRead
from app.schemas.referentiel import AgentRead, PurposeRead, ServiceRead
from app.schemas.sync import ParametresHorsLigne, PresentsHorsLigne, ReferentielsHorsLigne
from app.schemas.visit import VisitFilters, VisitRead
from app.services.setting_service import SettingService

router = APIRouter(prefix="/sync", tags=["Hors-ligne"], route_class=StrictQueryParamsRoute)

# Au-delà, la liste des présents est tronquée et le signale (`tronque`). Un poste
# d'accueil en compte quelques dizaines ; mille couvre un grand site sans
# transformer la réponse en export.
PRESENTS_MAX = 1000


def _organisation_requise(organization_id: uuid.UUID | None) -> uuid.UUID:
    """Le hors-ligne se prépare pour un poste, donc pour une organisation.

    Seul l'ADMIN de plateforme peut arriver ici sans : il doit la nommer.
    """
    if organization_id is None:
        raise OrganizationRequiredError(
            "Précisez `organization_id` : le hors-ligne se prépare pour une organisation."
        )
    return organization_id


def _etag_correspond(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidats = {c.strip().removeprefix("W/") for c in if_none_match.split(",")}
    return "*" in candidats or etag in candidats


@router.get(
    "/referentiels",
    response_model=ReferentielsHorsLigne,
    summary="Référentiels pour le travail hors-ligne",
    responses={304: {"description": "Rien n'a changé depuis la version envoyée."}},
)
async def referentiels_hors_ligne(
    session: SessionDep,
    organization_id: OrganizationFilter,
    response: Response,
    if_none_match: Annotated[str | None, Header()] = None,
) -> ReferentielsHorsLigne | Response:
    """Instantané des référentiels **actifs** de l'organisation.

    Le client remplace son cache en bloc. Un élément archivé disparaît de la
    liste : une visite hors-ligne qui le référence déjà sortira en
    `ARCHIVED_REFERENTIEL` à la synchro.
    """
    organisation_id = _organisation_requise(organization_id)
    organisation = await session.get(Organization, organisation_id)
    if organisation is None:
        raise OrganizationNotFoundError(details={"organization_id": str(organisation_id)})

    parametres = await SettingService(session).get_settings(organization_id=organisation_id)
    services = await ServiceRepository(session).list_all(organization_id=organisation_id)
    agents = await AgentRepository(session).list_all(organization_id=organisation_id)
    motifs = await PurposeRepository(session).list_all(organization_id=organisation_id)

    contenu = {
        "organization": OrganizationThemeRead.model_validate(organisation),
        "services": [ServiceRead.model_validate(s) for s in services],
        "agents": [AgentRead.model_validate(a) for a in agents],
        "purposes": [PurposeRead.model_validate(p) for p in motifs],
        "parametres": ParametresHorsLigne(
            automatic_visit_closure_enabled=parametres.automatic_visit_closure_enabled,
            automatic_visit_closure_time=parametres.automatic_visit_closure_time,
            visit_long_duration_alert_minutes=parametres.visit_long_duration_alert_minutes,
            server_ocr_enabled=parametres.server_ocr_enabled,
        ),
    }
    version = ReferentielsHorsLigne.empreinte(
        {
            cle: (
                [v.model_dump(mode="json") for v in valeur]
                if isinstance(valeur, list)
                else valeur.model_dump(mode="json")
            )
            for cle, valeur in contenu.items()
        }
    )
    etag = f'"{version}"'
    entetes = {"ETag": etag, "Cache-Control": "private, no-cache"}

    if _etag_correspond(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=entetes)

    response.headers.update(entetes)
    return ReferentielsHorsLigne(version=version, server_time=datetime.now(UTC), **contenu)


@router.get(
    "/presents",
    response_model=PresentsHorsLigne,
    summary="Visites ouvertes, pour le travail hors-ligne",
)
async def presents_hors_ligne(
    session: SessionDep, organization_id: OrganizationFilter
) -> PresentsHorsLigne:
    """Toutes les visites `PRESENT` de l'organisation, de la plus ancienne à la
    plus récente — celles que la tablette pourra clôturer sans réseau, quelle que
    soit la tablette qui les a enregistrées."""
    organisation_id = _organisation_requise(organization_id)
    filtres = VisitFilters(
        organization_id=organisation_id, statut=VisitStatus.PRESENT, sort="asc"
    )
    depot = VisitRepository(session)
    total = await depot.count(filtres)
    visites = await depot.list_paginated(filtres, limit=PRESENTS_MAX, offset=0)
    return PresentsHorsLigne(
        server_time=datetime.now(UTC),
        total=total,
        tronque=total > len(visites),
        items=[VisitRead.model_validate(v) for v in visites],
    )
