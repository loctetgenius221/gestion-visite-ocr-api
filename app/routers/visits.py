from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.deps import AdminUser, CurrentUser, VisitServiceDep
from app.models.enums import VisitStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.visit import (
    VisitCancelRequest,
    VisitCreate,
    VisitFilters,
    VisitRead,
    VisitSyncRequest,
    VisitSyncResponse,
    VisitUpdate,
)
from app.services.export_service import ExportFormat, export_filename

router = APIRouter(prefix="/visits", tags=["Visites"])


def get_visit_filters(
    statut: VisitStatus | None = Query(default=None),
    date_from: datetime | None = Query(default=None, description="Borne basse, `checked_in_at`."),
    date_to: datetime | None = Query(default=None, description="Borne haute, `checked_in_at`."),
    search: str | None = Query(
        default=None, max_length=100, description="Nom, prénom ou n° de document."
    ),
    sort: Literal["asc", "desc"] = Query(default="desc", description="Tri sur `checked_in_at`."),
    # Filtres du dashboard web, sans effet sur l'app mobile qui ne les envoie pas.
    service_id: uuid.UUID | None = Query(default=None, description="Service visité."),
    agent_id: uuid.UUID | None = Query(default=None, description="Personne rencontrée."),
    purpose_id: uuid.UUID | None = Query(default=None, description="Motif de la visite."),
    created_by: uuid.UUID | None = Query(
        default=None, description="Compte ayant enregistré la visite."
    ),
) -> VisitFilters:
    return VisitFilters(
        statut=statut,
        date_from=date_from,
        date_to=date_to,
        search=search,
        sort=sort,
        service_id=service_id,
        agent_id=agent_id,
        purpose_id=purpose_id,
        created_by=created_by,
    )


def get_pagination(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


@router.post(
    "",
    response_model=VisitRead,
    status_code=status.HTTP_201_CREATED,
    summary="Enregistre une nouvelle visite",
)
async def create_visit(
    payload: VisitCreate, service: VisitServiceDep, current_user: CurrentUser
) -> VisitRead:
    visit = await service.create_visit(payload, current_user)
    return VisitRead.model_validate(visit)


@router.get("", response_model=Page[VisitRead], summary="Liste paginée et filtrée des visites")
async def list_visits(
    service: VisitServiceDep,
    current_user: CurrentUser,
    filters: Annotated[VisitFilters, Depends(get_visit_filters)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
) -> Page[VisitRead]:
    return await service.list_visits(filters, pagination)


@router.post(
    "/sync",
    response_model=VisitSyncResponse,
    summary="Synchronisation batch des visites créées hors-ligne",
)
async def sync_visits(
    payload: VisitSyncRequest, service: VisitServiceDep, current_user: CurrentUser
) -> VisitSyncResponse:
    """Le batch est traité item par item : le résultat individuel de chaque insertion
    est renvoyé (`created` / `conflict` / `error`), la réponse globale reste 200."""
    return await service.sync_visits(payload.visits, current_user)


@router.get(
    "/export",
    summary="Exporte le registre des visites",
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {}}, "description": "Registre au format demandé"},
        501: {"description": "Format d'export non encore disponible"},
    },
)
async def export_visits(
    service: VisitServiceDep,
    current_admin: AdminUser,
    filters: Annotated[VisitFilters, Depends(get_visit_filters)],
    export_format: ExportFormat = Query(
        default="csv", alias="format", description="`csv` uniquement pour le moment."
    ),
) -> Response:
    """Les mêmes filtres que le listing s'appliquent, sans pagination.

    Le CSV est encodé en UTF-8 avec BOM et séparé par des points-virgules : c'est
    ce qu'attend Excel en configuration francophone. Au-delà du plafond
    `visits_export_max_rows` (paramètres système), l'export est refusé plutôt que
    tronqué en silence.
    """
    contenu, media_type = await service.export_visits(filters, export_format, current_admin)
    nom = export_filename(export_format, datetime.now(UTC))
    return Response(
        content=contenu,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


@router.get("/{visit_id}", response_model=VisitRead, summary="Détail d'une visite")
async def get_visit(
    visit_id: uuid.UUID, service: VisitServiceDep, current_user: CurrentUser
) -> VisitRead:
    return VisitRead.model_validate(await service.get_visit(visit_id))


@router.put("/{visit_id}/checkout", response_model=VisitRead, summary="Clôture une visite")
async def checkout_visit(
    visit_id: uuid.UUID, service: VisitServiceDep, current_user: CurrentUser
) -> VisitRead:
    return VisitRead.model_validate(await service.checkout(visit_id, current_user))


@router.patch(
    "/{visit_id}",
    response_model=VisitRead,
    summary="Corrige une visite (administrateur)",
)
async def update_visit(
    visit_id: uuid.UUID,
    payload: VisitUpdate,
    service: VisitServiceDep,
    current_admin: AdminUser,
) -> VisitRead:
    """Correction d'une erreur de saisie. Le champ `reason` est obligatoire : il part
    au journal d'audit avec le diff avant/après.

    L'identité du visiteur n'est pas modifiable ici — elle provient du scan MRZ.
    """
    return VisitRead.model_validate(await service.update_visit(visit_id, payload, current_admin))


@router.post(
    "/{visit_id}/cancel",
    response_model=VisitRead,
    summary="Annule une visite (administrateur)",
)
async def cancel_visit(
    visit_id: uuid.UUID,
    payload: VisitCancelRequest,
    service: VisitServiceDep,
    current_admin: AdminUser,
) -> VisitRead:
    """Annulation **logique** : la visite passe en `ANNULEE` et reste en base pour
    l'audit, avec son motif. Elle est exclue de toutes les statistiques."""
    return VisitRead.model_validate(
        await service.cancel_visit(visit_id, payload.reason, current_admin)
    )
