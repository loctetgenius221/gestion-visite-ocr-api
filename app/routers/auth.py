from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.deps import AuthServiceDep, CurrentUser
from app.core.logging import get_logger
from app.schemas.auth import (
    AccessTokenResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    TokenPair,
    UserRead,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentification"])


@router.post("/login", response_model=TokenPair, summary="Connexion d'un agent de contrôle")
async def login(payload: LoginRequest, auth_service: AuthServiceDep) -> TokenPair:
    return await auth_service.authenticate(payload.identifiant, payload.mot_de_passe)


@router.post(
    "/token",
    response_model=TokenPair,
    summary="Connexion au format OAuth2 (formulaire)",
)
async def login_oauth2(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth_service: AuthServiceDep,
) -> TokenPair:
    """Variante OAuth2 « password flow » de `/auth/login`.

    Même logique d'authentification, mais en `application/x-www-form-urlencoded`
    avec les champs standard `username`/`password` — `username` portant
    l'identifiant de l'agent. C'est l'endpoint déclaré comme `tokenUrl` : il rend le
    bouton « Authorize » de Swagger directement utilisable.

    Les clients (app Flutter) doivent préférer `/auth/login`, en JSON.
    """
    return await auth_service.authenticate(form_data.username, form_data.password)


@router.post("/refresh", response_model=AccessTokenResponse, summary="Renouvelle l'access token")
async def refresh(payload: RefreshRequest, auth_service: AuthServiceDep) -> AccessTokenResponse:
    return await auth_service.refresh(payload.refresh_token)


@router.post("/logout", response_model=MessageResponse, summary="Révoque le refresh token courant")
async def logout(payload: LogoutRequest, auth_service: AuthServiceDep) -> MessageResponse:
    await auth_service.logout(payload.refresh_token)
    return MessageResponse(message="Déconnexion effectuée.")


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Demande de réinitialisation du mot de passe",
)
async def forgot_password(payload: ForgotPasswordRequest) -> MessageResponse:
    """Stub : aucun service mail/SMS n'est configuré à ce stade (voir ADR-006).

    La route répond toujours 202 sans révéler si le compte existe, et se contente de
    tracer la demande — le branchement d'un fournisseur d'envoi se fera ici.
    """
    logger.info(
        "Demande de réinitialisation de mot de passe",
        extra={"identifiant": payload.identifiant},
    )
    return MessageResponse(
        message=(
            "Si ce compte existe, la procédure de réinitialisation "
            "a été transmise à l'administrateur."
        )
    )


@router.get("/me", response_model=UserRead, summary="Profil de l'utilisateur authentifié")
async def read_me_under_auth(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


# La spec §5.1 expose `/me` à la racine de l'API : router séparé pour éviter le
# préfixe `/auth`, tout en gardant l'alias ci-dessus par commodité.
me_router = APIRouter(tags=["Authentification"])


@me_router.get("/me", response_model=UserRead, summary="Profil de l'utilisateur authentifié")
async def read_me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)
