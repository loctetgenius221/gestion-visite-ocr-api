"""Schémas du journal d'audit (lecture seule, rôle ADMIN)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.audit_log import AuditLog


class AuditLogRead(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_identifiant: str | None = None
    action: str = Field(examples=["visit.cancelled", "user.created", "auth.login.failed"])
    entity: str = Field(examples=["visit", "user", "service"])
    entity_id: str | None = None
    metadata: dict[str, Any] | None = None
    ip_address: str | None = None
    created_at: datetime

    @classmethod
    def from_model(cls, log: AuditLog) -> AuditLogRead:
        """Construction explicite : l'attribut du modèle s'appelle `meta`.

        `metadata` est réservé par SQLAlchemy — il désigne l'objet `MetaData` de la
        classe de base — donc impossible comme nom d'attribut. La colonne SQL et le
        champ d'API, eux, s'appellent bien `metadata`.
        """
        return cls(
            id=log.id,
            actor_id=log.actor_id,
            actor_identifiant=log.actor_identifiant,
            action=log.action,
            entity=log.entity,
            entity_id=log.entity_id,
            metadata=log.meta,
            ip_address=log.ip_address,
            created_at=log.created_at,
        )


class AuditLogFilters(BaseModel):
    actor_id: uuid.UUID | None = None
    action: str | None = Field(default=None, max_length=80)
    entity: str | None = Field(default=None, max_length=50)
    entity_id: str | None = Field(default=None, max_length=64)
    date_from: datetime | None = None
    date_to: datetime | None = None
