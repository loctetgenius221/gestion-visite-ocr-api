"""Administration des comptes utilisateurs — réservé au rôle ADMIN.

Aucune route de suppression : un compte ayant enregistré des visites est
référencé en `ON DELETE RESTRICT`. La désactivation coupe l'accès sans amputer
le registre.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import AdminUser, UserAdminServiceDep
from app.models.enums import UserRole, UserStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.user import (
    PasswordResetRequest,
    SessionRead,
    UserAdminRead,
    UserCreate,
    UserCredentialsResponse,
    UserFilters,
    UserStatusUpdate,
    UserUpdate,
)

router = APIRouter(prefix="/users", tags=["Administration — Comptes"])


def get_user_filters(
    role: UserRole | None = Query(default=None),
    status_filter: UserStatus | None = Query(
        default=None,
        alias="status",
        description="`active` ou `inactive`.",
    ),
    search: str | None = Query(default=None, max_length=100, description="Nom ou identifiant."),
    sort: str = Query(default="desc", pattern="^(asc|desc)$", description="Tri sur la création."),
) -> UserFilters:
    return UserFilters(role=role, status=status_filter, search=search, sort=sort)  # type: ignore[arg-type]


def get_pagination(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


@router.get("", response_model=Page[UserAdminRead], summary="Liste paginée des comptes")
async def list_users(
    service: UserAdminServiceDep,
    current_admin: AdminUser,
    filters: Annotated[UserFilters, Depends(get_user_filters)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
) -> Page[UserAdminRead]:
    return await service.list_users(filters, pagination)


@router.post(
    "",
    response_model=UserCredentialsResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Crée un compte",
)
async def create_user(
    payload: UserCreate, service: UserAdminServiceDep, current_admin: AdminUser
) -> UserCredentialsResponse:
    """Sans `mot_de_passe`, le serveur en génère un et le renvoie **une seule fois**.

    Il n'est ni stocké en clair ni journalisé : transmettez-le à l'utilisateur par
    un canal sûr, il sera irrécupérable ensuite (seule une réinitialisation reste
    possible).
    """
    user, mot_de_passe = await service.create_user(payload, current_admin)
    return UserCredentialsResponse(
        user=UserAdminRead.model_validate(user), mot_de_passe=mot_de_passe
    )


@router.get("/{user_id}", response_model=UserAdminRead, summary="Détail d'un compte")
async def get_user(
    user_id: uuid.UUID, service: UserAdminServiceDep, current_admin: AdminUser
) -> UserAdminRead:
    return UserAdminRead.model_validate(await service.get_user(user_id))


@router.put("/{user_id}", response_model=UserAdminRead, summary="Modifie nom, poste ou rôle")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    service: UserAdminServiceDep,
    current_admin: AdminUser,
) -> UserAdminRead:
    """L'identifiant n'est pas modifiable : il rattache les visites à leur auteur."""
    return UserAdminRead.model_validate(await service.update_user(user_id, payload, current_admin))


@router.patch(
    "/{user_id}/status", response_model=UserAdminRead, summary="Active ou désactive un compte"
)
async def set_user_status(
    user_id: uuid.UUID,
    payload: UserStatusUpdate,
    service: UserAdminServiceDep,
    current_admin: AdminUser,
) -> UserAdminRead:
    """Désactiver révoque immédiatement toutes les sessions ouvertes du compte.

    Refusé sur son propre compte et sur le dernier administrateur actif : sans ce
    garde-fou, le dashboard peut devenir inaccessible sans accès direct à la base.
    """
    return UserAdminRead.model_validate(
        await service.set_status(user_id, payload.status, current_admin)
    )


@router.post(
    "/{user_id}/reset-password",
    response_model=UserCredentialsResponse,
    summary="Réinitialise le mot de passe",
)
async def reset_password(
    user_id: uuid.UUID,
    payload: PasswordResetRequest,
    service: UserAdminServiceDep,
    current_admin: AdminUser,
) -> UserCredentialsResponse:
    """Coupe aussi toutes les sessions : un mot de passe réinitialisé l'est souvent
    parce qu'il a fuité, et laisser vivre les sessions viderait l'opération de sens."""
    user, mot_de_passe = await service.reset_password(user_id, payload.mot_de_passe, current_admin)
    return UserCredentialsResponse(
        user=UserAdminRead.model_validate(user), mot_de_passe=mot_de_passe
    )


@router.post("/{user_id}/unlock", response_model=UserAdminRead, summary="Débloque un compte")
async def unlock_user(
    user_id: uuid.UUID, service: UserAdminServiceDep, current_admin: AdminUser
) -> UserAdminRead:
    """Remet à zéro le compteur d'échecs et lève le verrouillage avant son terme."""
    return UserAdminRead.model_validate(await service.unlock(user_id, current_admin))


@router.get(
    "/{user_id}/sessions",
    response_model=list[SessionRead],
    summary="Sessions actives du compte",
)
async def list_sessions(
    user_id: uuid.UUID, service: UserAdminServiceDep, current_admin: AdminUser
) -> list[SessionRead]:
    """Refresh tokens émis, ni révoqués ni expirés. Le token lui-même n'est pas stocké."""
    return await service.list_sessions(user_id)


@router.delete(
    "/{user_id}/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Révoque une session à distance",
)
async def revoke_session(
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    service: UserAdminServiceDep,
    current_admin: AdminUser,
) -> None:
    """Cas d'usage : tablette perdue ou volée. L'appareil est déconnecté au prochain
    renouvellement de son access token, soit au plus tard 30 minutes après."""
    await service.revoke_session(user_id, session_id, current_admin)
