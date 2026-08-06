from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.models.user import User
from app.repositories.user_repository import RevokedTokenRepository, UserRepository
from app.schemas.auth import AccessTokenResponse, TokenPair, UserRead

logger = get_logger(__name__)


class AuthService:
    """Logique d'authentification : login, refresh, logout, résolution du porteur du token."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.revoked = RevokedTokenRepository(session)

    async def authenticate(self, identifiant: str, mot_de_passe: str) -> TokenPair:
        user = await self.users.get_by_identifiant(identifiant)
        # Même erreur pour « utilisateur inconnu » et « mot de passe faux » : ne pas
        # révéler l'existence d'un compte.
        if user is None or not verify_password(mot_de_passe, user.mot_de_passe_hash):
            logger.warning("Echec d'authentification", extra={"identifiant": identifiant})
            raise InvalidCredentialsError()
        if not user.is_active:
            raise InactiveUserError()

        return self._issue_token_pair(user)

    def _issue_token_pair(self, user: User) -> TokenPair:
        claims = {"role": user.role.value, "identifiant": user.identifiant}
        access_token, _ = create_access_token(str(user.id), claims)
        refresh_token, _ = create_refresh_token(str(user.id))
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=UserRead.model_validate(user),
        )

    async def refresh(self, refresh_token: str) -> AccessTokenResponse:
        payload = decode_token(refresh_token, expected_type="refresh")
        if await self.revoked.is_revoked(payload.jti):
            raise InvalidTokenError("Ce refresh token a été révoqué.")

        user = await self._load_active_user(payload.subject)
        access_token, _ = create_access_token(
            str(user.id), {"role": user.role.value, "identifiant": user.identifiant}
        )
        return AccessTokenResponse(
            access_token=access_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def logout(self, refresh_token: str) -> None:
        """Révoque le refresh token courant en l'ajoutant à la liste de révocation."""
        payload = decode_token(refresh_token, expected_type="refresh")
        user = await self._load_active_user(payload.subject)
        await self.revoked.revoke(payload.jti, user.id, payload.expires_at)
        await self.session.commit()

    async def resolve_access_token(self, token: str) -> User:
        """Résout un access token vers l'utilisateur porteur (dépendance d'authentification)."""
        payload = decode_token(token, expected_type="access")
        return await self._load_active_user(payload.subject)

    async def _load_active_user(self, subject: str) -> User:
        try:
            user_id = uuid.UUID(subject)
        except ValueError as exc:
            raise InvalidTokenError() from exc

        user = await self.users.get_by_id(user_id)
        if user is None:
            raise InvalidTokenError()
        if not user.is_active:
            raise InactiveUserError()
        return user
