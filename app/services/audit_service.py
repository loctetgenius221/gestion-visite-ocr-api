"""Journal d'audit : qui a fait quoi, quand, et sur quoi.

Écrit dans la **même transaction** que l'opération auditée. C'est délibéré : une
visite annulée sans trace d'annulation serait pire qu'une annulation refusée. Si
le commit échoue, l'opération et sa trace disparaissent ensemble — jamais l'une
sans l'autre.

Seule exception, les échecs d'authentification : l'opération métier échoue par
définition, la trace doit pourtant survivre. `AuthService` les commit à part.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.user import User
from app.repositories.audit_repository import AuditLogRepository
from app.schemas.audit import AuditLogFilters, AuditLogRead
from app.schemas.common import Page, PaginationParams


class AuditAction:
    """Verbes du journal, en `<entité>.<événement>`.

    Regroupés ici plutôt que dispersés en littéraux : le filtre par préfixe du
    dashboard (`action=visit`) dépend de cette convention de nommage.
    """

    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILED = "auth.login.failed"
    LOGIN_LOCKED = "auth.login.locked"
    LOGOUT = "auth.logout"

    VISIT_CREATED = "visit.created"
    VISIT_UPDATED = "visit.updated"
    VISIT_CHECKOUT = "visit.checkout"
    VISIT_CANCELLED = "visit.cancelled"
    VISIT_EXPORTED = "visit.exported"

    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_STATUS_CHANGED = "user.status_changed"
    USER_PASSWORD_RESET = "user.password_reset"
    USER_UNLOCKED = "user.unlocked"
    USER_SESSION_REVOKED = "user.session_revoked"

    SERVICE_CREATED = "service.created"
    SERVICE_UPDATED = "service.updated"
    SERVICE_STATUS_CHANGED = "service.status_changed"
    AGENT_CREATED = "agent.created"
    AGENT_UPDATED = "agent.updated"
    AGENT_STATUS_CHANGED = "agent.status_changed"
    PURPOSE_CREATED = "purpose.created"
    PURPOSE_UPDATED = "purpose.updated"
    PURPOSE_STATUS_CHANGED = "purpose.status_changed"

    SETTINGS_UPDATED = "settings.updated"


@dataclass(frozen=True, slots=True)
class ClientContext:
    """Origine de la requête, pour enrichir la trace."""

    ip_address: str | None = None
    user_agent: str | None = None


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Champs réellement modifiés, sous la forme `{champ: {"avant": …, "après": …}}`.

    Seuls les écarts sont conservés : un diff qui recopie l'objet entier rend le
    journal illisible et grossit la base pour rien.
    """
    changements: dict[str, Any] = {}
    for champ, valeur_apres in after.items():
        valeur_avant = before.get(champ)
        if valeur_avant != valeur_apres:
            changements[champ] = {"avant": valeur_avant, "après": valeur_apres}
    return changements


def _serialisable(value: Any) -> Any:
    """Ramène une valeur à un type que la colonne JSON accepte."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serialisable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialisable(item) for item in value]
    if hasattr(value, "value"):  # énumérations
        return value.value
    return value


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.logs = AuditLogRepository(session)

    async def record(
        self,
        action: str,
        *,
        entity: str,
        entity_id: str | uuid.UUID | None = None,
        actor: User | None = None,
        actor_identifiant: str | None = None,
        metadata: dict[str, Any] | None = None,
        context: ClientContext | None = None,
    ) -> AuditLog:
        """Enregistre un événement. Ne committe pas : l'appelant maîtrise sa transaction."""
        return await self.logs.add(
            AuditLog(
                actor_id=actor.id if actor is not None else None,
                actor_identifiant=(
                    actor_identifiant
                    if actor_identifiant is not None
                    else (actor.identifiant if actor is not None else None)
                ),
                action=action,
                entity=entity,
                entity_id=str(entity_id) if entity_id is not None else None,
                meta=_serialisable(metadata) if metadata else None,
                ip_address=context.ip_address if context else None,
            )
        )

    async def list_logs(
        self, filters: AuditLogFilters, pagination: PaginationParams
    ) -> Page[AuditLogRead]:
        total = await self.logs.count(filters)
        items = await self.logs.list_paginated(
            filters, limit=pagination.page_size, offset=pagination.offset
        )
        return Page[AuditLogRead](
            items=[AuditLogRead.from_model(log) for log in items],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )

    async def list_actions(self) -> list[str]:
        return await self.logs.distinct_actions()


def now() -> datetime:
    return datetime.now(UTC)
