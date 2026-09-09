"""Administration des organisations et structures.

Deux niveaux d'accès, appliqués par le `TenantScope` injecté et non par des tests
recopiés route par route :

- l'**administrateur de plateforme** crée les structures, les active, les
  désactive et voit tout le parc ;
- l'**administrateur d'organisation** gère la sienne — identité, charte
  graphique, administrateurs, comptes — et rien d'autre.

Aucune suppression n'est exposée : services, visites et comptes référencent une
organisation en `ON DELETE RESTRICT`, et effacer une structure ferait perdre le
registre qu'elle a produit. La désactivation coupe l'accès sans amputer l'histoire.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.core.deps import AdminScope, OrganizationServiceDep, SuperAdminUser, UserAdminServiceDep
from app.core.routing import StrictQueryParamsRoute
from app.models.enums import UserRole, UserStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.organization import (
    OrganizationAdminCreate,
    OrganizationCreate,
    OrganizationFilters,
    OrganizationRead,
    OrganizationStats,
    OrganizationStatusUpdate,
    OrganizationThemeRead,
    OrganizationUpdate,
    OrganizationWithAdminResponse,
)
from app.schemas.user import UserAdminRead, UserCredentialsResponse, UserFilters

router = APIRouter(
    prefix="/organizations",
    tags=["Organisations & Structures"],
    route_class=StrictQueryParamsRoute,
)

# Route publique : la page de connexion doit porter les couleurs de la structure
# avant que quiconque ait pu s'identifier.
public_router = APIRouter(
    prefix="/organizations",
    tags=["Organisations & Structures"],
    route_class=StrictQueryParamsRoute,
)


def get_organization_filters(
    search: Annotated[
        str | None, Query(max_length=100, description="Nom, code ou localisation.")
    ] = None,
    is_active: Annotated[bool | None, Query(description="Filtre par état d'activation.")] = None,
    sort: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    sort_by: Annotated[str, Query(pattern="^(nom|created_at)$")] = "nom",
) -> OrganizationFilters:
    return OrganizationFilters(
        search=search,
        is_active=is_active,
        sort=sort,  # type: ignore[arg-type]
        sort_by=sort_by,  # type: ignore[arg-type]
    )


def get_pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


# --- Charte graphique publique ----------------------------------------------


@public_router.get(
    "/theme",
    response_model=OrganizationThemeRead,
    summary="Charte graphique d'une organisation (public)",
)
async def get_public_theme(
    org_service: OrganizationServiceDep,
    code: Annotated[
        str, Query(max_length=50, description="Code de la structure, ex: MIN-INTERIEUR.")
    ],
) -> OrganizationThemeRead:
    """Couleurs, logo et nom d'une structure **active**, sans authentification.

    C'est la seule route d'organisation ouverte : la page de connexion doit
    s'afficher aux couleurs de la structure, et elle s'affiche par définition
    avant tout token. Elle ne renvoie que ce qui est déjà visible sur une
    devanture — jamais de contact, de statistique ni d'état d'activation ; une
    structure désactivée y répond 404, comme une structure inexistante.
    """
    organization = await org_service.get_theme(code)
    return OrganizationThemeRead.model_validate(organization)


# --- Lecture -----------------------------------------------------------------


@router.get("", response_model=Page[OrganizationRead], summary="Liste des organisations")
async def list_organizations(
    org_service: OrganizationServiceDep,
    scope: AdminScope,
    filters: Annotated[OrganizationFilters, Depends(get_organization_filters)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
) -> Page[OrganizationRead]:
    """Liste paginée, filtrable et triable.

    Un administrateur d'organisation n'y voit que la sienne : la route reste
    appelable pour lui, ce qui évite au dashboard d'avoir deux écrans distincts.
    """
    return await org_service.list_organizations(filters, pagination, scope)


@router.get("/{organization_id}", response_model=OrganizationRead, summary="Détail")
async def get_organization(
    organization_id: uuid.UUID,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> OrganizationRead:
    return await org_service.get_organization(organization_id, scope)


@router.get(
    "/{organization_id}/stats",
    response_model=OrganizationStats,
    summary="Volumétrie d'une organisation",
)
async def get_organization_stats(
    organization_id: uuid.UUID,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> OrganizationStats:
    """Services, agents, comptes, visites totales et visites en cours."""
    return await org_service.get_stats(organization_id, scope)


@router.get(
    "/{organization_id}/theme",
    response_model=OrganizationThemeRead,
    summary="Charte graphique (authentifié)",
)
async def get_organization_theme(
    organization_id: uuid.UUID,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> OrganizationThemeRead:
    organization = await org_service.get_organization(organization_id, scope)
    return OrganizationThemeRead.model_validate(organization)


@router.get(
    "/{organization_id}/administrators",
    response_model=list[UserAdminRead],
    summary="Administrateurs de l'organisation",
)
async def list_organization_administrators(
    organization_id: uuid.UUID,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> list[UserAdminRead]:
    """Comptes `ORG_ADMIN`, actifs en tête.

    Les comptes désactivés sont inclus : mieux vaut réactiver un administrateur
    existant que d'en recréer un et laisser traîner l'ancien.
    """
    return await org_service.list_administrators(organization_id, scope)


@router.get(
    "/{organization_id}/users",
    response_model=Page[UserAdminRead],
    summary="Comptes de l'organisation",
)
async def list_organization_users(
    organization_id: uuid.UUID,
    org_service: OrganizationServiceDep,
    user_service: UserAdminServiceDep,
    scope: AdminScope,
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
    role: Annotated[UserRole | None, Query(description="Filtre par rôle.")] = None,
    user_status: Annotated[
        UserStatus | None, Query(alias="status", description="`active` ou `inactive`.")
    ] = None,
    search: Annotated[str | None, Query(max_length=100, description="Nom ou identifiant.")] = None,
) -> Page[UserAdminRead]:
    """Même moteur que `GET /users`, borné à une organisation.

    Existe pour que le dashboard ouvre la fiche d'une structure et y trouve ses
    comptes, sans avoir à composer un filtre sur une autre page.
    """
    # Vérifie l'existence **et** le périmètre avant de déléguer : sans cela, un
    # identifiant d'organisation inconnu renverrait une page vide au lieu d'un 404.
    await org_service.get_organization(organization_id, scope)
    filters = UserFilters(
        organization_id=organization_id, role=role, status=user_status, search=search
    )
    return await user_service.list_users(filters, pagination, scope.user)


# --- Écriture ----------------------------------------------------------------


@router.post(
    "",
    response_model=OrganizationWithAdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crée une organisation",
)
async def create_organization(
    payload: OrganizationCreate,
    org_service: OrganizationServiceDep,
    current_admin: SuperAdminUser,
) -> OrganizationWithAdminResponse:
    """Réservé à l'administrateur de plateforme.

    `admin_nom` et `admin_identifiant` créent dans la foulée le premier
    administrateur de la structure. Sans mot de passe fourni, le serveur en génère
    un et le renvoie dans `admin_temporary_password` — **une seule fois** : il
    n'est stocké nulle part en clair.
    """
    return await org_service.create_organization(payload, current_admin)


@router.put("/{organization_id}", response_model=OrganizationRead, summary="Modifie")
async def update_organization(
    organization_id: uuid.UUID,
    payload: OrganizationUpdate,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> OrganizationRead:
    """Mise à jour partielle : seuls les champs **présents** dans le corps bougent.

    Un `null` explicite efface le champ — c'est ainsi qu'on retire une
    localisation ou une couleur d'accent. `is_active` n'est pas ici (voir
    `PATCH /status`), et `logo_url` non plus (voir `POST /logo`).
    """
    return await org_service.update_organization(organization_id, payload, scope)


@router.put(
    "/{organization_id}/theme",
    response_model=OrganizationRead,
    summary="Modifie la charte graphique",
)
async def update_organization_theme(
    organization_id: uuid.UUID,
    payload: OrganizationUpdate,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> OrganizationRead:
    """Alias de `PUT /{id}` centré sur les couleurs et `theme_config`.

    Le dashboard sépare l'écran « identité » de l'écran « apparence » ; les deux
    écrivent la même ressource.
    """
    return await org_service.update_organization(organization_id, payload, scope)


@router.patch(
    "/{organization_id}/status",
    response_model=OrganizationRead,
    summary="Active ou désactive une organisation",
)
async def set_organization_status(
    organization_id: uuid.UUID,
    payload: OrganizationStatusUpdate,
    org_service: OrganizationServiceDep,
    current_admin: SuperAdminUser,
    scope: AdminScope,
) -> OrganizationRead:
    """Réservé à l'administrateur de plateforme.

    Désactiver coupe l'accès de **tous** les comptes rattachés dès la requête
    suivante, sessions déjà ouvertes comprises. L'opération est refusée
    (`409 ORGANIZATION_HAS_ACTIVE_VISITS`) tant que des visiteurs sont présents :
    les agents ne pourraient plus enregistrer leur sortie, et le registre
    resterait ouvert sur des visites qu'aucune route ne saurait plus clôturer.
    """
    return await org_service.set_status(organization_id, payload, scope)


@router.post(
    "/{organization_id}/administrators",
    response_model=UserCredentialsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Nomme un administrateur",
)
async def add_organization_administrator(
    organization_id: uuid.UUID,
    payload: OrganizationAdminCreate,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> UserCredentialsResponse:
    """Crée un compte `ORG_ADMIN` rattaché à l'organisation.

    Sans `mot_de_passe`, le serveur en génère un et le renvoie **une seule
    fois** : transmettez-le par un canal sûr, il sera irrécupérable ensuite.
    """
    admin, mot_de_passe = await org_service.add_administrator(organization_id, payload, scope)
    return UserCredentialsResponse(user=admin, mot_de_passe=mot_de_passe)


# --- Logo --------------------------------------------------------------------


@router.post(
    "/{organization_id}/logo",
    response_model=OrganizationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Dépose le logo de l'organisation",
)
async def upload_organization_logo(
    organization_id: uuid.UUID,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
    logo: Annotated[UploadFile, File(description="PNG, JPEG, WebP ou GIF. 2 Mo maximum.")],
) -> OrganizationRead:
    """Valide le fichier par son **contenu**, le réencode en PNG, puis le stocke.

    Le format est déduit des octets et non de l'extension : un `.png` n'est qu'un
    nom, et un document HTML ainsi renommé, stocké puis servi depuis l'origine du
    dashboard, s'y exécuterait avec la session de qui l'ouvre. Le réencodage
    reconstruit l'image à partir de ses seuls pixels, ce qui écarte au passage les
    métadonnées où l'octet arbitraire se cache d'ordinaire.

    L'image est ramenée à 512 px sur son plus grand côté et convertie en PNG, qui
    préserve la transparence — un logo aplati sur du blanc est inutilisable sur un
    fond sombre. Le logo précédent est supprimé une fois le nouveau en place.

    `logo_url` n'est écrit que par cette route : il n'est pas modifiable via
    `PUT /{id}`, faute de quoi un administrateur pourrait faire pointer chaque
    écran du dashboard vers une ressource tierce.
    """
    contenu = await logo.read()
    return await org_service.set_logo(organization_id, contenu, scope)


@router.delete(
    "/{organization_id}/logo",
    response_model=OrganizationRead,
    summary="Retire le logo de l'organisation",
)
async def delete_organization_logo(
    organization_id: uuid.UUID,
    org_service: OrganizationServiceDep,
    scope: AdminScope,
) -> OrganizationRead:
    """Efface le fichier et remet `logo_url` à `null`. Sans logo, la route ne fait rien."""
    return await org_service.remove_logo(organization_id, scope)
