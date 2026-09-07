from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ForgottenDocumentStatus
from app.schemas.auth import UserRead
from app.schemas.common import ORMModel
from app.schemas.visit import VisitorRead


class ForgottenDocumentCaseRead(ORMModel):
    """Fiche de suivi d'une pièce potentiellement oubliée."""

    id: uuid.UUID
    organization_id: uuid.UUID | None = None
    visit_id: uuid.UUID
    visitor_id: uuid.UUID
    status: ForgottenDocumentStatus
    resolved_at: datetime | None = None
    resolved_by: uuid.UUID | None = None
    resolution_note: str | None = None
    sms_status: str | None = None
    sms_sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    visitor: VisitorRead
    resolved_by_user: UserRead | None = None


class ForgottenDocumentCaseResolve(BaseModel):
    """Action de résolution : la pièce a été remise ou restituée au visiteur."""

    model_config = ConfigDict(str_strip_whitespace=True)

    resolution_note: str | None = Field(
        default=None,
        max_length=500,
        description="Commentaire optionnel sur la restitution de la pièce.",
    )


class ForgottenDocumentFilters(BaseModel):
    """Filtres pour l'historique des pièces oubliées."""

    organization_id: uuid.UUID | None = None
    status: ForgottenDocumentStatus | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    search: str | None = Field(
        default=None,
        max_length=100,
        description="Nom, prénom ou numéro de pièce du visiteur.",
    )
    sort: Literal["asc", "desc"] = "desc"
