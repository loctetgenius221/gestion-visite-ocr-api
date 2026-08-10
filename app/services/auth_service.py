from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    InactiveUserError,
    InvalidCredentialsError,
    InvalidTokenError,
    LockedAccountError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.models.user import User
from app.repositories.user_repository import (
    RevokedTokenRepository,
    UserRepository,
    UserSessionRepository,
)
from app.schemas.auth import AccessTokenResponse, TokenPair, UserRead
from app.services.audit_service import AuditAction, AuditService, ClientContext
from app.services.setting_service import SettingService

logger = get_logger(__name__)


class AuthService:
    """Logique d'authentification : login, refresh, logout, résolution du porteur du token."""

    def __init__(self, session: AsyncSession, context: ClientContext | None = None) -> None:
        self.session = session
        self.context = context or ClientContext()
        self.users = UserRepository(session)
        self.revoked = RevokedTokenRepository(session)
        self.sessions = UserSessionRepository(session)
        self.audit = AuditService(session)
        self.settings_service = SettingService(session)

    async def authenticate(self, identifiant: str, mot_de_passe: str) -> TokenPair:
        """Vérifie les identifiants et ouvre une session.

        Le verrouillage après N échecs consécutifs est le seul rempart applicatif
        contre le bourrage de mots de passe — le proxy limite déjà le débit, mais
        rien n'empêche une attaque lente et distribuée. N et la durée sont des
        paramètres système, modifiables sans redéploiement.
        """
        maintenant = datetime.now(UTC)
        user = await self.users.get_by_identifiant(identifiant)

        if user is None:
            # Même erreur que pour un mot de passe faux : ne jamais révéler
            # l'existence d'un compte.
            await self._tracer_echec(identifiant, raison="compte_inconnu")
            raise InvalidCredentialsError()

        if user.is_locked(maintenant):
            await self._tracer_echec(
                identifiant, raison="compte_verrouille", user=user, action=AuditAction.LOGIN_LOCKED
            )
            raise LockedAccountError(details={"locked_until": user.locked_until})

        if not verify_password(mot_de_passe, user.mot_de_passe_hash):
            await self._enregistrer_echec(user)
            raise InvalidCredentialsError()

        if not user.is_active:
            await self._tracer_echec(identifiant, raison="compte_desactive", user=user)
            raise InactiveUserError()

        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = maintenant

        pair = await self._issue_token_pair(user)
        await self.audit.record(
            AuditAction.LOGIN_SUCCESS,
            entity="user",
            entity_id=user.id,
            actor=user,
            context=self.context,
        )
        await self.session.commit()
        return pair

    async def _issue_token_pair(self, user: User) -> TokenPair:
        claims = {"role": user.role.value, "identifiant": user.identifiant}
        access_token, _ = create_access_token(str(user.id), claims)
        refresh_token, refresh_payload = create_refresh_token(str(user.id))

        # Trace la session : sans elle, impossible de lister les appareils
        # connectés ni de couper l'accès d'une tablette perdue.
        await self.sessions.open(
            user_id=user.id,
            jti=refresh_payload.jti,
            expires_at=refresh_payload.expires_at,
            user_agent=self.context.user_agent,
            ip_address=self.context.ip_address,
        )

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=UserRead.model_validate(user),
        )

    async def _enregistrer_echec(self, user: User) -> None:
        """Incrémente le compteur d'échecs et verrouille le compte au seuil.

        Committe explicitement : l'appelant va lever une exception, et sans commit
        le compteur serait perdu au rollback — le verrouillage ne se déclencherait
        jamais.
        """
        parametres = await self.settings_service.get_settings()
        user.failed_login_attempts += 1

        verrouille = user.failed_login_attempts >= parametres.max_failed_login_attempts
        if verrouille:
            user.locked_until = datetime.now(UTC) + timedelta(
                minutes=parametres.account_lockout_minutes
            )

        logger.warning(
            "Echec d'authentification",
            extra={
                "identifiant": user.identifiant,
                "tentatives": user.failed_login_attempts,
                "verrouille": verrouille,
            },
        )
        await self.audit.record(
            AuditAction.LOGIN_FAILED,
            entity="user",
            entity_id=user.id,
            actor_identifiant=user.identifiant,
            metadata={
                "tentatives": user.failed_login_attempts,
                "compte_verrouille": verrouille,
            },
            context=self.context,
        )
        await self.session.commit()

    async def _tracer_echec(
        self,
        identifiant: str,
        *,
        raison: str,
        user: User | None = None,
        action: str = AuditAction.LOGIN_FAILED,
    ) -> None:
        """Trace un échec qui n'incrémente aucun compteur (compte inconnu, verrouillé…)."""
        logger.warning("Echec d'authentification", extra={"identifiant": identifiant})
        await self.audit.record(
            action,
            entity="user",
            entity_id=user.id if user is not None else None,
            actor_identifiant=identifiant,
            metadata={"raison": raison},
            context=self.context,
        )
        await self.session.commit()

    async def refresh(self, refresh_token: str) -> AccessTokenResponse:
        payload = decode_token(refresh_token, expected_type="refresh")
        if await self.revoked.is_revoked(payload.jti):
            raise InvalidTokenError("Ce refresh token a été révoqué.")

        user = await self._load_active_user(payload.subject)
        access_token, _ = create_access_token(
            str(user.id), {"role": user.role.value, "identifiant": user.identifiant}
        )
        await self.sessions.touch(payload.jti, datetime.now(UTC))
        await self.session.commit()
        return AccessTokenResponse(
            access_token=access_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def logout(self, refresh_token: str) -> None:
        """Révoque le refresh token courant en l'ajoutant à la liste de révocation."""
        payload = decode_token(refresh_token, expected_type="refresh")
        user = await self._load_active_user(payload.subject)
        await self.revoked.revoke(payload.jti, user.id, payload.expires_at)
        await self.sessions.revoke_by_jti(payload.jti, datetime.now(UTC))
        await self.audit.record(
            AuditAction.LOGOUT,
            entity="user",
            entity_id=user.id,
            actor=user,
            context=self.context,
        )
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
