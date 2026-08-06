from __future__ import annotations

import uuid

from app.schemas.common import ORMModel


class ServiceRead(ORMModel):
    id: uuid.UUID
    name: str
    code: str
    floor: str | None = None
    parent_id: uuid.UUID | None = None


class ServiceTree(ServiceRead):
    """Service avec ses sous-services, pour un rendu hiérarchique côté Flutter."""

    children: list[ServiceTree] = []


class AgentRead(ORMModel):
    id: uuid.UUID
    name: str
    role: str | None = None
    office: str | None = None
    service_id: uuid.UUID


class PurposeRead(ORMModel):
    id: uuid.UUID
    libelle: str
