from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import AutoClosureServiceDep, CurrentUser, Scope
from app.models.enums import ForgottenDocumentStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.forgotten_document import (
    ForgottenDocumentCaseRead,
    ForgottenDocumentCaseResolve,
    ForgottenDocumentFilters,
)

router = APIRouter(prefix="/forgotten-document-cases", tags=["Pièces oubliées"])


@router.get(
    "",
    response_model=Page[ForgottenDocumentCaseRead],
    summary="Lister les pièces oubliées",
)
async def list_cases(
    service: AutoClosureServiceDep,
    scope: Scope,
    organization_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Organisation concernée. Réservé à l'administrateur de plateforme : "
            "les autres comptes sont bornés à la leur."
        ),
    ),
    status: ForgottenDocumentStatus | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    search: str | None = Query(default=None, max_length=100),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Page[ForgottenDocumentCaseRead]:
    filters = ForgottenDocumentFilters(
        organization_id=scope.resolve(organization_id),
        status=status,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )
    return await service.list_forgotten_cases(
        filters, PaginationParams(page=page, page_size=page_size)
    )


@router.get(
    "/{case_id}",
    response_model=ForgottenDocumentCaseRead,
    summary="Détail d'une pièce oubliée",
)
async def get_case(
    case_id: uuid.UUID,
    service: AutoClosureServiceDep,
    scope: Scope,
) -> ForgottenDocumentCaseRead:
    return ForgottenDocumentCaseRead.model_validate(
        await service.get_case(case_id, scope.organization_id)
    )


@router.post(
    "/{case_id}/resolve",
    response_model=ForgottenDocumentCaseRead,
    summary="Marquer une pièce comme récupérée",
)
async def resolve_case(
    case_id: uuid.UUID,
    payload: ForgottenDocumentCaseResolve,
    service: AutoClosureServiceDep,
    current_user: CurrentUser,
) -> ForgottenDocumentCaseRead:
    return ForgottenDocumentCaseRead.model_validate(
        await service.resolve_case(case_id, payload, current_user)
    )
