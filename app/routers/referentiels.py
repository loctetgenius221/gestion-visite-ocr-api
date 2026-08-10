"""Référentiels : lecture ouverte à tout compte authentifié, écriture réservée à ADMIN.

L'app mobile ne voit que l'actif ; le dashboard peut demander l'archivé avec
`include_archived=true`. Aucun `DELETE` n'est exposé : les visites référencent ces
enregistrements, seule l'archive est possible.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.core.deps import AdminUser, CurrentUser, ReferentielAdminServiceDep, SessionDep
from app.core.errors import ServiceNotFoundError
from app.models.referentiel import Service
from app.repositories.referentiel_repository import (
    AgentRepository,
    PurposeRepository,
    ServiceRepository,
)
from app.schemas.referentiel import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    PurposeCreate,
    PurposeRead,
    PurposeUpdate,
    RecordStatusUpdate,
    ServiceCreate,
    ServiceRead,
    ServiceTree,
    ServiceUpdate,
)

router = APIRouter(tags=["Référentiels"])
admin_router = APIRouter(tags=["Administration — Référentiels"])

_INCLURE_ARCHIVES = Query(
    default=False,
    description="Inclut les entrées archivées. Réservé au dashboard d'administration.",
)


def _build_tree(services: list[Service]) -> list[ServiceTree]:
    """Reconstruit la hiérarchie des services en une passe, sans requête N+1."""
    # On construit les nœuds champ par champ plutôt que par `model_validate` sur
    # l'objet ORM : Pydantic irait sinon chercher l'attribut `children`, ce qui
    # déclencherait un lazy load interdit en contexte async.
    nodes = {
        service.id: ServiceTree(
            id=service.id,
            name=service.name,
            code=service.code,
            floor=service.floor,
            parent_id=service.parent_id,
            is_archived=service.is_archived,
            archived_at=service.archived_at,
            children=[],
        )
        for service in services
    }

    roots: list[ServiceTree] = []
    for service in services:
        node = nodes[service.id]
        parent = nodes.get(service.parent_id) if service.parent_id else None
        # Un parent absent de la liste (donnée orpheline, ou parent archivé alors
        # que l'enfant ne l'est pas) ne doit pas faire disparaître le service : il
        # est remonté à la racine.
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)
    return roots


# --- Lecture (tout compte authentifié) ---------------------------------------


@router.get("/services", response_model=list[ServiceTree], summary="Liste des services")
async def list_services(
    session: SessionDep,
    current_user: CurrentUser,
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[ServiceTree]:
    services = await ServiceRepository(session).list_all(include_archived=include_archived)
    return _build_tree(services)


@router.get(
    "/services/{service_id}/agents",
    response_model=list[AgentRead],
    summary="Agents rattachés à un service",
)
async def list_service_agents(
    service_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[AgentRead]:
    if not await ServiceRepository(session).exists(service_id):
        raise ServiceNotFoundError(details={"service_id": str(service_id)})
    agents = await AgentRepository(session).list_all(
        service_id=service_id, include_archived=include_archived
    )
    return [AgentRead.model_validate(agent) for agent in agents]


@router.get("/agents", response_model=list[AgentRead], summary="Liste des agents du ministère")
async def list_agents(
    session: SessionDep,
    current_user: CurrentUser,
    service_id: uuid.UUID | None = Query(default=None, description="Filtre par service."),
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[AgentRead]:
    agents = await AgentRepository(session).list_all(
        service_id=service_id, include_archived=include_archived
    )
    return [AgentRead.model_validate(agent) for agent in agents]


@router.get("/purposes", response_model=list[PurposeRead], summary="Motifs de visite")
async def list_purposes(
    session: SessionDep,
    current_user: CurrentUser,
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[PurposeRead]:
    purposes = await PurposeRepository(session).list_all(include_archived=include_archived)
    return [PurposeRead.model_validate(purpose) for purpose in purposes]


# --- Écriture (rôle ADMIN) ---------------------------------------------------


@admin_router.post(
    "/services",
    response_model=ServiceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crée un service",
)
async def create_service(
    payload: ServiceCreate, service: ReferentielAdminServiceDep, current_admin: AdminUser
) -> ServiceRead:
    return ServiceRead.model_validate(await service.create_service(payload, current_admin))


@admin_router.put(
    "/services/{service_id}", response_model=ServiceRead, summary="Modifie un service"
)
async def update_service(
    service_id: uuid.UUID,
    payload: ServiceUpdate,
    service: ReferentielAdminServiceDep,
    current_admin: AdminUser,
) -> ServiceRead:
    return ServiceRead.model_validate(
        await service.update_service(service_id, payload, current_admin)
    )


@admin_router.patch(
    "/services/{service_id}/status",
    response_model=ServiceRead,
    summary="Archive ou désarchive un service",
)
async def set_service_status(
    service_id: uuid.UUID,
    payload: RecordStatusUpdate,
    service: ReferentielAdminServiceDep,
    current_admin: AdminUser,
) -> ServiceRead:
    """Archiver est refusé tant que le service porte des agents ou des sous-services
    actifs : ils resteraient sélectionnables sans service visible côté mobile."""
    return ServiceRead.model_validate(
        await service.set_service_status(service_id, payload.status, current_admin)
    )


@admin_router.post(
    "/agents",
    response_model=AgentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crée un agent",
)
async def create_agent(
    payload: AgentCreate, service: ReferentielAdminServiceDep, current_admin: AdminUser
) -> AgentRead:
    return AgentRead.model_validate(await service.create_agent(payload, current_admin))


@admin_router.put("/agents/{agent_id}", response_model=AgentRead, summary="Modifie un agent")
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    service: ReferentielAdminServiceDep,
    current_admin: AdminUser,
) -> AgentRead:
    return AgentRead.model_validate(await service.update_agent(agent_id, payload, current_admin))


@admin_router.patch(
    "/agents/{agent_id}/status",
    response_model=AgentRead,
    summary="Archive ou désarchive un agent",
)
async def set_agent_status(
    agent_id: uuid.UUID,
    payload: RecordStatusUpdate,
    service: ReferentielAdminServiceDep,
    current_admin: AdminUser,
) -> AgentRead:
    return AgentRead.model_validate(
        await service.set_agent_status(agent_id, payload.status, current_admin)
    )


@admin_router.post(
    "/purposes",
    response_model=PurposeRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crée un motif de visite",
)
async def create_purpose(
    payload: PurposeCreate, service: ReferentielAdminServiceDep, current_admin: AdminUser
) -> PurposeRead:
    return PurposeRead.model_validate(await service.create_purpose(payload, current_admin))


@admin_router.put(
    "/purposes/{purpose_id}", response_model=PurposeRead, summary="Modifie un motif de visite"
)
async def update_purpose(
    purpose_id: uuid.UUID,
    payload: PurposeUpdate,
    service: ReferentielAdminServiceDep,
    current_admin: AdminUser,
) -> PurposeRead:
    return PurposeRead.model_validate(
        await service.update_purpose(purpose_id, payload, current_admin)
    )


@admin_router.patch(
    "/purposes/{purpose_id}/status",
    response_model=PurposeRead,
    summary="Archive ou désarchive un motif",
)
async def set_purpose_status(
    purpose_id: uuid.UUID,
    payload: RecordStatusUpdate,
    service: ReferentielAdminServiceDep,
    current_admin: AdminUser,
) -> PurposeRead:
    return PurposeRead.model_validate(
        await service.set_purpose_status(purpose_id, payload.status, current_admin)
    )
