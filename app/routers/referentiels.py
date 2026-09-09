"""Référentiels : lecture ouverte à tout compte authentifié, écriture réservée à ADMIN.

L'app mobile ne voit que l'actif ; le dashboard peut demander l'archivé avec
`include_archived=true`. Aucun `DELETE` n'est exposé : les visites référencent ces
enregistrements, seule l'archive est possible.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status

from app.core.deps import (
    AdminScope,
    AdminUser,
    OrganizationFilter,
    ReferentielAdminServiceDep,
    ReferentielImportServiceDep,
    Scope,
    SessionDep,
)
from app.core.errors import ServiceNotFoundError
from app.core.routing import StrictQueryParamsRoute
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
from app.schemas.referentiel_import import (
    AgentImportRequest,
    AgentImportRow,
    ImportReport,
    ServiceImportRequest,
    ServiceImportRow,
)
from app.services.referentiel_import_service import ReferentielImportService

router = APIRouter(tags=["Référentiels"], route_class=StrictQueryParamsRoute)
admin_router = APIRouter(tags=["Administration — Référentiels"], route_class=StrictQueryParamsRoute)

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
    organization_id: OrganizationFilter,
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[ServiceTree]:
    """Services de votre organisation. L'administrateur de plateforme les voit tous,
    ou ceux d'une seule structure avec `organization_id`."""
    services = await ServiceRepository(session).list_all(
        organization_id=organization_id, include_archived=include_archived
    )
    return _build_tree(services)


@router.get(
    "/services/{service_id}/agents",
    response_model=list[AgentRead],
    summary="Agents rattachés à un service",
)
async def list_service_agents(
    service_id: uuid.UUID,
    session: SessionDep,
    scope: Scope,
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[AgentRead]:
    service_repo = ServiceRepository(session)
    service = await service_repo.get_by_id(service_id)
    if service is None:
        raise ServiceNotFoundError(details={"service_id": str(service_id)})
    scope.ensure_can_access(service.organization_id)
    agents = await AgentRepository(session).list_all(
        service_id=service_id, include_archived=include_archived
    )
    return [AgentRead.model_validate(agent) for agent in agents]


@router.get("/agents", response_model=list[AgentRead], summary="Liste des agents du ministère")
async def list_agents(
    session: SessionDep,
    organization_id: OrganizationFilter,
    service_id: uuid.UUID | None = Query(default=None, description="Filtre par service."),
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[AgentRead]:
    """Personnel de votre organisation. L'administrateur de plateforme voit tout
    l'annuaire, ou celui d'une seule structure avec `organization_id`."""
    agents = await AgentRepository(session).list_all(
        service_id=service_id,
        organization_id=organization_id,
        include_archived=include_archived,
    )
    return [AgentRead.model_validate(agent) for agent in agents]


@router.get("/purposes", response_model=list[PurposeRead], summary="Motifs de visite")
async def list_purposes(
    session: SessionDep,
    organization_id: OrganizationFilter,
    include_archived: bool = _INCLURE_ARCHIVES,
) -> list[PurposeRead]:
    """Motifs communs à la plateforme, plus ceux propres à votre organisation.

    Sans `organization_id`, l'administrateur de plateforme voit **tout** le
    référentiel ; avec, il voit ce que verrait cette structure — ses motifs et les
    motifs communs."""
    purposes = await PurposeRepository(session).list_all(
        organization_id=organization_id, include_archived=include_archived
    )
    return [PurposeRead.model_validate(purpose) for purpose in purposes]


# --- Import en masse (rôles ADMIN et ORG_ADMIN) ------------------------------
#
# Trois routes par référentiel, dans l'ordre où le dashboard les enchaîne :
# télécharger le modèle, prévisualiser un classeur rempli, appliquer.
#
# La prévisualisation reçoit le fichier, l'application reçoit du JSON. Ce n'est
# pas une asymétrie de confort : le fichier ne peut plus changer entre ce que
# l'administrateur a vu et ce qu'il confirme, et la grille du dashboard devient
# corrigeable ligne à ligne sans repasser par le tableur.

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_FICHIER = File(description="Classeur `.xlsx` rempli à partir du modèle.")
_ORGANISATION_IMPORT = Query(
    default=None,
    description=(
        "Organisation qui reçoit l'import. Obligatoire pour l'ADMIN de plateforme "
        "(`400 ORGANIZATION_REQUIRED` sinon) ; ignoré pour un administrateur "
        "d'organisation, qui n'importe que dans la sienne."
    ),
)
_METTRE_A_JOUR = Query(
    default=False,
    description=(
        "Met à jour les entrées déjà présentes. À `false`, elles ressortent "
        "`unchanged` et ne sont pas touchées."
    ),
)


def _reponse_modele(contenu: bytes, nom: str) -> Response:
    """Sert le classeur en pièce jointe, sous son vrai nom.

    `filename` et `filename*` sont émis tous les deux : la forme historique pour
    les clients qui ne lisent que celle-là, la forme RFC 5987 pour les autres.
    Un client qui ignore l'en-tête enregistrera tout de même le fichier sous un
    nom de son cru — c'est arrivé, en `.csv` — d'où `verifier_classeur`, qui juge
    l'import sur les octets et non sur l'extension.
    """
    return Response(
        content=contenu,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{nom}"; filename*=UTF-8\'\'{nom}'
            )
        },
    )


@admin_router.get(
    "/services/import/template",
    summary="Modèle .xlsx pour l'import des services",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Classeur modèle"}},
)
async def download_service_import_template(scope: AdminScope) -> Response:
    """Classeur vide à remplir : une feuille de saisie, une feuille de notice.

    Les colonnes sont au format texte, pour qu'un code de service saisi « 007 » ne
    devienne pas le nombre 7 sous les doigts du tableur. La feuille de saisie ne
    contient aucun exemple — laissé en place, il finirait importé ; ils sont sur
    la notice.

    L'organisation n'est pas une colonne : elle se choisit au téléversement. Un
    même modèle sert donc à toutes les structures.
    """
    return _reponse_modele(ReferentielImportService.modele_services(), "modele-services.xlsx")


@admin_router.get(
    "/agents/import/template",
    summary="Modèle .xlsx pour l'import des agents",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Classeur modèle"}},
)
async def download_agent_import_template(scope: AdminScope) -> Response:
    """Chaque ligne rattache une personne à un service **par son code**.

    Le code, et non l'identifiant : personne ne recopie un UUID dans un tableur.
    Le service doit exister au moment de l'import — commencez par le modèle des
    services si l'annuaire est vierge.
    """
    return _reponse_modele(ReferentielImportService.modele_agents(), "modele-agents.xlsx")


@admin_router.post(
    "/services/import/preview",
    response_model=ImportReport[ServiceImportRow],
    summary="Prévisualise l'import d'un classeur de services",
)
async def preview_service_import(
    service: ReferentielImportServiceDep,
    scope: AdminScope,
    fichier: Annotated[UploadFile, _FICHIER],
    organization_id: uuid.UUID | None = _ORGANISATION_IMPORT,
    mettre_a_jour: bool = _METTRE_A_JOUR,
) -> ImportReport[ServiceImportRow]:
    """Lit le classeur et rend le plan. **Rien n'est écrit.**

    Chaque ligne ressort en `create`, `update`, `unchanged` ou `error`, avec son
    numéro de ligne d'origine pour que l'administrateur retrouve la cellule
    fautive. Le rapport porte aussi le nom de l'organisation visée : un import
    massif dans la mauvaise structure ne doit pas se découvrir après coup.

    Les rattachements parent/enfant sont résolus sur le fichier **et** la base :
    une ligne peut désigner comme parent un service créé plus bas dans le même
    classeur, l'ordre des lignes est indifférent. Un cycle est refusé — la
    hiérarchie est reparcourue à chaque `GET /services`, et une boucle y
    tournerait sans fin.
    """
    lignes = await service.lire_services(await fichier.read(), filename=fichier.filename)
    return await service.planifier_services(
        lignes, scope.user, organization_id=organization_id, mettre_a_jour=mettre_a_jour
    )


@admin_router.post(
    "/services/import",
    response_model=ImportReport[ServiceImportRow],
    summary="Applique un import de services",
)
async def import_services(
    payload: ServiceImportRequest, service: ReferentielImportServiceDep, scope: AdminScope
) -> ImportReport[ServiceImportRow]:
    """Applique les lignes validées, **en une seule transaction**.

    Une seule ligne en erreur suffit à tout refuser (`422 IMPORT_PLAN_REJECTED`),
    et rien n'est écrit. C'est l'inverse de `POST /visits/sync`, qui commite item
    par item — la différence est délibérée : là-bas, les visites n'existent que
    sur la tablette et ne peuvent pas être renvoyées ; ici, un classeur se corrige
    et se retéléverse.

    Rien n'est jamais supprimé ni archivé : un service absent du fichier reste en
    place. Un import partiel ne doit pas pouvoir amputer l'annuaire.
    """
    return await service.importer_services(
        payload.lignes,
        scope.user,
        organization_id=payload.organization_id,
        mettre_a_jour=payload.mettre_a_jour,
    )


@admin_router.post(
    "/agents/import/preview",
    response_model=ImportReport[AgentImportRow],
    summary="Prévisualise l'import d'un classeur d'agents",
)
async def preview_agent_import(
    service: ReferentielImportServiceDep,
    scope: AdminScope,
    fichier: Annotated[UploadFile, _FICHIER],
    organization_id: uuid.UUID | None = _ORGANISATION_IMPORT,
    mettre_a_jour: bool = _METTRE_A_JOUR,
) -> ImportReport[AgentImportRow]:
    """Lit le classeur et rend le plan. **Rien n'est écrit.**

    Un code de service inconnu dans l'organisation sort en `error` sur sa ligne
    (`SERVICE_NOT_FOUND`) : l'import d'agents ne crée jamais de service au
    passage, sans quoi une faute de frappe fabriquerait une direction fantôme.
    Un service archivé est refusé de la même façon qu'à la création unitaire.
    """
    lignes = await service.lire_agents(await fichier.read(), filename=fichier.filename)
    return await service.planifier_agents(
        lignes, scope.user, organization_id=organization_id, mettre_a_jour=mettre_a_jour
    )


@admin_router.post(
    "/agents/import",
    response_model=ImportReport[AgentImportRow],
    summary="Applique un import d'agents",
)
async def import_agents(
    payload: AgentImportRequest, service: ReferentielImportServiceDep, scope: AdminScope
) -> ImportReport[AgentImportRow]:
    """Applique les lignes validées, en une seule transaction. Voir `/services/import`."""
    return await service.importer_agents(
        payload.lignes,
        scope.user,
        organization_id=payload.organization_id,
        mettre_a_jour=payload.mettre_a_jour,
    )


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
