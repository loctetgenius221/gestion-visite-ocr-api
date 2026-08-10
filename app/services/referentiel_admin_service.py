"""Écriture sur les référentiels (services, agents, motifs) — rôle ADMIN.

Aucun `DELETE` n'est exposé. Les visites référencent ces enregistrements en
`ON DELETE RESTRICT` : une suppression échouerait sur la contrainte, ou pire,
amputerait l'historique du registre si la contrainte venait à sauter. Archiver
retire l'entrée des listes proposées à l'agent, sans toucher au passé.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AgentNotFoundError,
    ConflictError,
    DuplicateReferentielError,
    PurposeNotFoundError,
    ServiceNotFoundError,
)
from app.models.enums import RecordStatus
from app.models.referentiel import Agent, Purpose, Service
from app.models.user import User
from app.repositories.referentiel_repository import (
    AgentRepository,
    PurposeRepository,
    ServiceRepository,
)
from app.schemas.referentiel import (
    AgentCreate,
    AgentUpdate,
    PurposeCreate,
    PurposeUpdate,
    ServiceCreate,
    ServiceUpdate,
)
from app.services.audit_service import AuditAction, AuditService, diff


class ReferentielAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.services = ServiceRepository(session)
        self.agents = AgentRepository(session)
        self.purposes = PurposeRepository(session)
        self.audit = AuditService(session)

    # --- Services ------------------------------------------------------------

    async def create_service(self, payload: ServiceCreate, actor: User) -> Service:
        existant = await self.services.get_by_code(payload.code)
        if existant is not None:
            raise DuplicateReferentielError(
                "Un service porte déjà ce code.", details={"code": payload.code}
            )
        await self._verifier_parent(payload.parent_id)

        service = await self.services.add(
            Service(
                name=payload.name,
                code=payload.code,
                floor=payload.floor,
                parent_id=payload.parent_id,
            )
        )
        await self.audit.record(
            AuditAction.SERVICE_CREATED,
            entity="service",
            entity_id=service.id,
            actor=actor,
            metadata={"code": service.code, "name": service.name},
        )
        await self.session.commit()
        return service

    async def update_service(
        self, service_id: uuid.UUID, payload: ServiceUpdate, actor: User
    ) -> Service:
        service = await self._get_service(service_id)
        modifications = payload.model_dump(exclude_unset=True)

        if "code" in modifications and modifications["code"]:
            autre = await self.services.get_by_code(modifications["code"])
            if autre is not None and autre.id != service.id:
                raise DuplicateReferentielError(
                    "Un autre service porte déjà ce code.",
                    details={"code": modifications["code"]},
                )

        if "parent_id" in modifications:
            await self._verifier_parent(modifications["parent_id"], enfant_id=service.id)

        avant = {
            "name": service.name,
            "code": service.code,
            "floor": service.floor,
            "parent_id": service.parent_id,
        }
        for champ, valeur in modifications.items():
            # `parent_id` accepte `None` (remise à la racine) ; les autres champs
            # ne sont pas effaçables par un `null`.
            if valeur is not None or champ == "parent_id":
                setattr(service, champ, valeur)
        apres = {
            "name": service.name,
            "code": service.code,
            "floor": service.floor,
            "parent_id": service.parent_id,
        }

        await self.audit.record(
            AuditAction.SERVICE_UPDATED,
            entity="service",
            entity_id=service.id,
            actor=actor,
            metadata={"changements": diff(avant, apres)},
        )
        await self.session.commit()
        return service

    async def set_service_status(
        self, service_id: uuid.UUID, status: RecordStatus, actor: User
    ) -> Service:
        service = await self._get_service(service_id)

        if status is RecordStatus.ARCHIVED:
            # Archiver un service dont dépendent des agents actifs les rendrait
            # sélectionnables sans service visible côté mobile.
            agents_actifs = await self.services.count_agents(service.id)
            if agents_actifs:
                raise ConflictError(
                    "Ce service compte encore des agents actifs : archivez-les d'abord.",
                    error_code="SERVICE_HAS_ACTIVE_AGENTS",
                    details={"agents_actifs": agents_actifs},
                )
            sous_services = await self.services.count_children(service.id)
            if sous_services:
                raise ConflictError(
                    "Ce service compte encore des sous-services actifs.",
                    error_code="SERVICE_HAS_ACTIVE_CHILDREN",
                    details={"sous_services_actifs": sous_services},
                )

        self._appliquer_statut(service, status)
        await self.audit.record(
            AuditAction.SERVICE_STATUS_CHANGED,
            entity="service",
            entity_id=service.id,
            actor=actor,
            metadata={"status": status.value},
        )
        await self.session.commit()
        return service

    # --- Agents --------------------------------------------------------------

    async def create_agent(self, payload: AgentCreate, actor: User) -> Agent:
        service = await self._get_service(payload.service_id)
        if service.is_archived:
            raise ConflictError(
                "Impossible de rattacher un agent à un service archivé.",
                error_code="ARCHIVED_REFERENTIEL",
                details={"service_id": str(service.id)},
            )
        if await self.agents.get_by_name_in_service(payload.name, payload.service_id) is not None:
            raise DuplicateReferentielError(
                "Un agent de ce nom existe déjà dans ce service.",
                details={"name": payload.name, "service_id": str(payload.service_id)},
            )

        agent = await self.agents.add(
            Agent(
                name=payload.name,
                role=payload.role,
                office=payload.office,
                service_id=payload.service_id,
            )
        )
        await self.audit.record(
            AuditAction.AGENT_CREATED,
            entity="agent",
            entity_id=agent.id,
            actor=actor,
            metadata={"name": agent.name, "service_id": agent.service_id},
        )
        await self.session.commit()
        return agent

    async def update_agent(
        self, agent_id: uuid.UUID, payload: AgentUpdate, actor: User
    ) -> Agent:
        agent = await self._get_agent(agent_id)
        modifications = payload.model_dump(exclude_unset=True, exclude_none=True)

        if "service_id" in modifications:
            service = await self._get_service(modifications["service_id"])
            if service.is_archived:
                raise ConflictError(
                    "Impossible de rattacher un agent à un service archivé.",
                    error_code="ARCHIVED_REFERENTIEL",
                    details={"service_id": str(service.id)},
                )

        avant = {
            "name": agent.name,
            "role": agent.role,
            "office": agent.office,
            "service_id": agent.service_id,
        }
        for champ, valeur in modifications.items():
            setattr(agent, champ, valeur)
        apres = {
            "name": agent.name,
            "role": agent.role,
            "office": agent.office,
            "service_id": agent.service_id,
        }

        await self.audit.record(
            AuditAction.AGENT_UPDATED,
            entity="agent",
            entity_id=agent.id,
            actor=actor,
            metadata={"changements": diff(avant, apres)},
        )
        await self.session.commit()
        return agent

    async def set_agent_status(
        self, agent_id: uuid.UUID, status: RecordStatus, actor: User
    ) -> Agent:
        agent = await self._get_agent(agent_id)
        self._appliquer_statut(agent, status)
        await self.audit.record(
            AuditAction.AGENT_STATUS_CHANGED,
            entity="agent",
            entity_id=agent.id,
            actor=actor,
            metadata={"status": status.value},
        )
        await self.session.commit()
        return agent

    # --- Motifs --------------------------------------------------------------

    async def create_purpose(self, payload: PurposeCreate, actor: User) -> Purpose:
        if await self.purposes.get_by_libelle(payload.libelle) is not None:
            raise DuplicateReferentielError(
                "Ce motif existe déjà.", details={"libelle": payload.libelle}
            )

        purpose = await self.purposes.add(Purpose(libelle=payload.libelle))
        await self.audit.record(
            AuditAction.PURPOSE_CREATED,
            entity="purpose",
            entity_id=purpose.id,
            actor=actor,
            metadata={"libelle": purpose.libelle},
        )
        await self.session.commit()
        return purpose

    async def update_purpose(
        self, purpose_id: uuid.UUID, payload: PurposeUpdate, actor: User
    ) -> Purpose:
        purpose = await self._get_purpose(purpose_id)
        if payload.libelle is not None and payload.libelle != purpose.libelle:
            autre = await self.purposes.get_by_libelle(payload.libelle)
            if autre is not None and autre.id != purpose.id:
                raise DuplicateReferentielError(
                    "Un autre motif porte déjà ce libellé.",
                    details={"libelle": payload.libelle},
                )
            avant = purpose.libelle
            purpose.libelle = payload.libelle
            await self.audit.record(
                AuditAction.PURPOSE_UPDATED,
                entity="purpose",
                entity_id=purpose.id,
                actor=actor,
                metadata={"changements": {"libelle": {"avant": avant, "après": purpose.libelle}}},
            )

        await self.session.commit()
        return purpose

    async def set_purpose_status(
        self, purpose_id: uuid.UUID, status: RecordStatus, actor: User
    ) -> Purpose:
        purpose = await self._get_purpose(purpose_id)
        self._appliquer_statut(purpose, status)
        await self.audit.record(
            AuditAction.PURPOSE_STATUS_CHANGED,
            entity="purpose",
            entity_id=purpose.id,
            actor=actor,
            metadata={"status": status.value},
        )
        await self.session.commit()
        return purpose

    # --- Aides ---------------------------------------------------------------

    @staticmethod
    def _appliquer_statut(record: Service | Agent | Purpose, status: RecordStatus) -> None:
        archive = status is RecordStatus.ARCHIVED
        if record.is_archived == archive:
            return
        record.is_archived = archive
        record.archived_at = datetime.now(UTC) if archive else None

    async def _get_service(self, service_id: uuid.UUID) -> Service:
        service = await self.services.get_by_id(service_id)
        if service is None:
            raise ServiceNotFoundError(details={"service_id": str(service_id)})
        return service

    async def _get_agent(self, agent_id: uuid.UUID) -> Agent:
        agent = await self.agents.get_by_id(agent_id)
        if agent is None:
            raise AgentNotFoundError(details={"agent_id": str(agent_id)})
        return agent

    async def _get_purpose(self, purpose_id: uuid.UUID) -> Purpose:
        purpose = await self.purposes.get_by_id(purpose_id)
        if purpose is None:
            raise PurposeNotFoundError(details={"purpose_id": str(purpose_id)})
        return purpose

    async def _verifier_parent(
        self, parent_id: uuid.UUID | None, *, enfant_id: uuid.UUID | None = None
    ) -> None:
        """Le parent doit exister, et ne pas appartenir au sous-arbre de l'enfant.

        Sans ce second contrôle, un service peut devenir son propre ancêtre : la
        reconstruction de l'arbre au listing partirait alors en boucle infinie.
        """
        if parent_id is None:
            return
        await self._get_service(parent_id)
        if enfant_id is None:
            return
        if parent_id in await self.services.descendant_ids(enfant_id):
            raise ConflictError(
                "Ce rattachement créerait un cycle dans la hiérarchie des services.",
                error_code="SERVICE_HIERARCHY_CYCLE",
                details={"service_id": str(enfant_id), "parent_id": str(parent_id)},
            )
