from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, SessionDep
from app.core.errors import ServiceNotFoundError
from app.models.referentiel import Service
from app.repositories.referentiel_repository import (
    AgentRepository,
    PurposeRepository,
    ServiceRepository,
)
from app.schemas.referentiel import AgentRead, PurposeRead, ServiceTree

router = APIRouter(tags=["Référentiels"])


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
            children=[],
        )
        for service in services
    }

    roots: list[ServiceTree] = []
    for service in services:
        node = nodes[service.id]
        parent = nodes.get(service.parent_id) if service.parent_id else None
        # Un parent absent de la liste (donnée orpheline) ne doit pas faire disparaître
        # le service : il est remonté à la racine.
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)
    return roots


@router.get("/services", response_model=list[ServiceTree], summary="Liste des services")
async def list_services(session: SessionDep, current_user: CurrentUser) -> list[ServiceTree]:
    services = await ServiceRepository(session).list_all()
    return _build_tree(services)


@router.get(
    "/services/{service_id}/agents",
    response_model=list[AgentRead],
    summary="Agents rattachés à un service",
)
async def list_service_agents(
    service_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> list[AgentRead]:
    if not await ServiceRepository(session).exists(service_id):
        raise ServiceNotFoundError(details={"service_id": str(service_id)})
    agents = await AgentRepository(session).list_all(service_id=service_id)
    return [AgentRead.model_validate(agent) for agent in agents]


@router.get("/agents", response_model=list[AgentRead], summary="Liste des agents du ministère")
async def list_agents(
    session: SessionDep,
    current_user: CurrentUser,
    service_id: uuid.UUID | None = Query(default=None, description="Filtre par service."),
) -> list[AgentRead]:
    agents = await AgentRepository(session).list_all(service_id=service_id)
    return [AgentRead.model_validate(agent) for agent in agents]


@router.get("/purposes", response_model=list[PurposeRead], summary="Motifs de visite")
async def list_purposes(session: SessionDep, current_user: CurrentUser) -> list[PurposeRead]:
    purposes = await PurposeRepository(session).list_all()
    return [PurposeRead.model_validate(purpose) for purpose in purposes]
