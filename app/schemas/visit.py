from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.enums import DocumentType, Sexe, VisitStatus
from app.schemas.auth import UserRead
from app.schemas.common import ORMModel
from app.schemas.referentiel import AgentRead, PurposeRead, ServiceRead


class VisitorInput(BaseModel):
    """Identité du visiteur, pré-remplie par l'OCR MRZ puis corrigeable par l'agent."""

    prenom: str = Field(min_length=1, max_length=150)
    nom: str = Field(min_length=1, max_length=150)
    type_document: DocumentType
    numero_document: str = Field(min_length=1, max_length=60)
    nin: str | None = Field(
        default=None,
        max_length=30,
        description=(
            "Numéro d'Identification National. Absent du MRZ des CNI sénégalaises "
            "(voir ADR-005) : à saisir manuellement depuis le verso de la carte."
        ),
    )
    nationalite: str | None = Field(default=None, max_length=60)
    date_naissance: date | None = None
    sexe: Sexe | None = None
    date_expiration_document: date | None = None
    telephone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    provenance: str | None = Field(default=None, max_length=200)
    immatriculation_vehicule: str | None = Field(default=None, max_length=40)
    mrz_image_url: str | None = Field(default=None, max_length=500)


class VisitorRead(ORMModel):
    id: uuid.UUID
    prenom: str
    nom: str
    type_document: DocumentType
    numero_document: str
    nin: str | None = None
    nationalite: str | None = None
    date_naissance: date | None = None
    sexe: Sexe | None = None
    date_expiration_document: date | None = None
    telephone: str | None = None
    email: str | None = None
    provenance: str | None = None
    immatriculation_vehicule: str | None = None
    mrz_image_url: str | None = None


class VisitCreate(BaseModel):
    """Payload de création d'une visite complète (visiteur + contexte)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    visitor: VisitorInput
    service_id: uuid.UUID
    agent_id: uuid.UUID
    purpose_id: uuid.UUID | None = None
    motif_libre: str | None = Field(default=None, max_length=500)
    badge_number: str | None = Field(default=None, max_length=50)
    signature_url: str | None = Field(default=None, max_length=500)
    # Horodatage d'entrée : fourni par le client en mode hors-ligne, sinon `now()` serveur.
    checked_in_at: datetime | None = None
    # Clé d'idempotence pour la synchronisation batch (voir ADR-004).
    client_reference: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _motif_requis(self) -> VisitCreate:
        """Un motif est obligatoire : soit une entrée du référentiel, soit du texte libre."""
        if self.purpose_id is None and not (self.motif_libre and self.motif_libre.strip()):
            raise ValueError("Un motif est requis : renseignez `purpose_id` ou `motif_libre`.")
        return self


class VisitRead(ORMModel):
    id: uuid.UUID
    statut: VisitStatus
    motif_libre: str | None = None
    badge_number: str | None = None
    signature_url: str | None = None
    checked_in_at: datetime
    checked_out_at: datetime | None = None
    client_reference: str | None = None
    created_at: datetime
    updated_at: datetime

    visitor: VisitorRead
    service: ServiceRead
    agent: AgentRead
    purpose: PurposeRead | None = None
    checked_in_user: UserRead
    checked_out_user: UserRead | None = None


class VisitFilters(BaseModel):
    statut: VisitStatus | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    search: str | None = Field(default=None, max_length=100)
    sort: Literal["asc", "desc"] = "desc"


class VisitSyncRequest(BaseModel):
    visits: list[VisitCreate] = Field(min_length=1, max_length=200)


class VisitSyncItemResult(BaseModel):
    index: int = Field(description="Position de la visite dans le batch envoyé.")
    client_reference: str | None = None
    status: Literal["created", "conflict", "error"]
    visit_id: uuid.UUID | None = None
    error_code: str | None = None
    message: str | None = None


class VisitSyncResponse(BaseModel):
    total: int
    created: int
    conflicts: int
    errors: int
    results: list[VisitSyncItemResult]
