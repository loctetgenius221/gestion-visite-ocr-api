from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import CurrentUser, VisitServiceDep
from app.models.enums import VisitStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.visit import (
    VisitCreate,
    VisitFilters,
    VisitRead,
    VisitSyncRequest,
    VisitSyncResponse,
)

router = APIRouter(prefix="/visits", tags=["Visites"])


def get_visit_filters(
    statut: VisitStatus | None = Query(default=None),
    date_from: datetime | None = Query(default=None, description="Borne basse, `checked_in_at`."),
    date_to: datetime | None = Query(default=None, description="Borne haute, `checked_in_at`."),
    search: str | None = Query(
        default=None, max_length=100, description="Nom, prénom ou n° de document."
    ),
    sort: Literal["asc", "desc"] = Query(default="desc", description="Tri sur `checked_in_at`."),
) -> VisitFilters:
    return VisitFilters(
        statut=statut, date_from=date_from, date_to=date_to, search=search, sort=sort
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
