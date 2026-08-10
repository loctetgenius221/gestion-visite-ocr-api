from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import RecordStatus
from app.schemas.common import ORMModel


class ArchivableRead(ORMModel):
    """Partie commune des référentiels : leur état d'archivage."""

    is_archived: bool = False
    archived_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> RecordStatus:
        return RecordStatus.ARCHIVED if self.is_archived else RecordStatus.ACTIVE


class ServiceRead(ArchivableRead):
    id: uuid.UUID
    name: str
    code: str
    floor: str | None = None
    parent_id: uuid.UUID | None = None


class ServiceTree(ServiceRead):
    """Service avec ses sous-services, pour un rendu hiérarchique côté Flutter."""

    children: list[ServiceTree] = []


class AgentRead(ArchivableRead):
    id: uuid.UUID
    name: str
    role: str | None = None
    office: str | None = None
    service_id: uuid.UUID


class PurposeRead(ArchivableRead):
    id: uuid.UUID
    libelle: str


# --- Écriture (rôle ADMIN) ---------------------------------------------------


class ServiceCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=50)
    floor: str | None = Field(default=None, max_length=50)
    parent_id: uuid.UUID | None = None


class ServiceUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    floor: str | None = Field(default=None, max_length=50)
    parent_id: uuid.UUID | None = None


class AgentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    service_id: uuid.UUID
    role: str | None = Field(default=None, max_length=200)
    office: str | None = Field(default=None, max_length=100)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    service_id: uuid.UUID | None = None
    role: str | None = Field(default=None, max_length=200)
    office: str | None = Field(default=None, max_length=100)


class PurposeCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    libelle: str = Field(min_length=1, max_length=200)


class PurposeUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    libelle: str | None = Field(default=None, min_length=1, max_length=200)


class RecordStatusUpdate(BaseModel):
    """Archivage / désarchivage. Aucune suppression physique n'est exposée."""

    status: RecordStatus
