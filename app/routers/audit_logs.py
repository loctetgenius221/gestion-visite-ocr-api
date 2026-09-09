"""Journal d'audit — lecture seule, réservé aux administrateurs.

Aucune route d'écriture ni de suppression n'est exposée : les entrées sont
produites par les services au fil des opérations, et une trace modifiable ne
vaudrait rien lors d'un audit.

Un administrateur d'organisation n'y lit **que le journal de son organisation** ;
l'administrateur de plateforme voit tout, y compris les événements globaux
(échecs de connexion de comptes non rattachés, administration des structures).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import AdminScope, SessionDep
from app.core.routing import StrictQueryParamsRoute
from app.schemas.audit import AuditLogFilters, AuditLogRead
from app.schemas.common import Page, PaginationParams
from app.services.audit_service import AuditService

router = APIRouter(
    prefix="/audit-logs",
    tags=["Administration — Audit"],
    route_class=StrictQueryParamsRoute,
)


def get_audit_filters(
    organization_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Organisation concernée. Réservé à l'administrateur de plateforme : "
            "un administrateur d'organisation est borné à la sienne."
        ),
    ),
    actor_id: uuid.UUID | None = Query(default=None, description="Auteur de l'action."),
    action: str | None = Query(
        default=None,
        max_length=80,
        description="Filtre par préfixe : `visit` ramène `visit.created`, `visit.cancelled`…",
    ),
    entity: str | None = Query(default=None, max_length=50, description="`visit`, `user`, …"),
    entity_id: str | None = Query(default=None, max_length=64),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
) -> AuditLogFilters:
    return AuditLogFilters(
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        date_from=date_from,
        date_to=date_to,
    )


def get_pagination(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


@router.get("", response_model=Page[AuditLogRead], summary="Liste paginée des événements")
async def list_audit_logs(
    session: SessionDep,
    scope: AdminScope,
    filters: Annotated[AuditLogFilters, Depends(get_audit_filters)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
) -> Page[AuditLogRead]:
    """Tri antéchronologique. `metadata` porte le diff avant/après le cas échéant."""
    filters = filters.model_copy(
        update={"organization_id": scope.resolve(filters.organization_id)}
    )
    return await AuditService(session).list_logs(filters, pagination)


@router.get(
    "/actions",
    response_model=list[str],
    summary="Actions présentes dans le journal",
)
async def list_audit_actions(session: SessionDep, scope: AdminScope) -> list[str]:
    """Alimente la liste déroulante du filtre côté dashboard, sans la coder en dur."""
    return await AuditService(session).list_actions(organization_id=scope.organization_id)
