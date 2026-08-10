"""Administration des comptes utilisateurs (dashboard web, rôle ADMIN).

Aucune suppression physique n'est exposée : un compte ayant enregistré des
visites est référencé par `visits.checked_in_by` en `ON DELETE RESTRICT`. Le
désactiver coupe l'accès sans amputer le registre.
"""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    DuplicateIdentifiantError,
    LastAdminError,
    SelfModificationError,
    SessionNotFoundError,
    UserNotFoundError,
)
from app.core.logging import get_logger
from app.core.security import hash_password
from app.models.enums import UserRole, UserStatus
from app.models.user import User
from app.repositories.user_repository import (
    RevokedTokenRepository,
    UserRepository,
    UserSessionRepository,
)
from app.schemas.common import Page, PaginationParams
from app.schemas.user import (
    SessionRead,
    UserAdminRead,
    UserCreate,
    UserFilters,
    UserUpdate,
)
from app.services.audit_service import AuditService, diff

logger = get_logger(__name__)

# Alphabet sans caractères ambigus : un mot de passe transmis oralement ou recopié
# depuis un écran ne doit pas se jouer sur un O contre un 0.
_ALPHABET = (
    "".join(c for c in string.ascii_uppercase if c not in "IO")
    + "".join(c for c in string.ascii_lowercase if c not in "l")
    + "".join(c for c in string.digits if c not in "01")
    + "!@#$%*-_"
)
GENERATED_PASSWORD_LENGTH = 16


def generate_password(length: int = GENERATED_PASSWORD_LENGTH) -> str:
    """Mot de passe aléatoire cryptographiquement sûr.

    `secrets` et non `random` : ce dernier s'appuie sur un Mersenne Twister dont
    l'état se reconstitue à partir de quelques tirages observés.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


class UserAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.sessions = UserSessionRepository(session)
        self.revoked = RevokedTokenRepository(session)
        self.audit = AuditService(session)

    # --- Lecture -------------------------------------------------------------

    async def list_users(
        self, filters: UserFilters, pagination: PaginationParams
    ) -> Page[UserAdminRead]:
        total = await self.users.count(filters)
        items = await self.users.list_paginated(
            filters, limit=pagination.page_size, offset=pagination.offset
        )
        return Page[UserAdminRead](
            items=[UserAdminRead.model_validate(user) for user in items],
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
        )

    async def get_user(self, user_id: uuid.UUID) -> User:
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(details={"user_id": str(user_id)})
        return user

    # --- Écriture ------------------------------------------------------------

    async def create_user(self, payload: UserCreate, actor: User) -> tuple[User, str | None]:
        """Crée un compte. Retourne `(compte, mot de passe en clair ou None)`.

        Le mot de passe en clair n'est renvoyé que s'il a été **généré** par le
        serveur : c'est l'unique occasion de le lire, il n'est stocké nulle part.
        """
        if await self.users.get_by_identifiant(payload.identifiant) is not None:
            raise DuplicateIdentifiantError(details={"identifiant": payload.identifiant})

        genere = payload.mot_de_passe is None
        mot_de_passe = payload.mot_de_passe or generate_password()

        user = User(
            nom=payload.nom,
            identifiant=payload.identifiant,
            poste=payload.poste,
            role=payload.role,
            mot_de_passe_hash=hash_password(mot_de_passe),
            is_active=True,
        )

        try:
            await self.users.add(user)
            await self.audit.record(
                "user.created",
                entity="user",
                entity_id=user.id,
                actor=actor,
                metadata={"identifiant": user.identifiant, "role": user.role},
            )
            await self.session.commit()
        except IntegrityError as exc:
            # Course entre deux créations simultanées du même identifiant : la
            # contrainte d'unicité tranche, on la traduit dans notre format.
            await self.session.rollback()
            raise DuplicateIdentifiantError(details={"identifiant": payload.identifiant}) from exc

        return await self.get_user(user.id), (mot_de_passe if genere else None)

    async def update_user(self, user_id: uuid.UUID, payload: UserUpdate, actor: User) -> User:
        user = await self.get_user(user_id)
        modifications = payload.model_dump(exclude_unset=True, exclude_none=True)

        if "role" in modifications and modifications["role"] is not UserRole.ADMIN:
            await self._refuser_si_dernier_admin(user, actor, operation="rétrograder")

        avant = {"nom": user.nom, "poste": user.poste, "role": user.role}
        for champ, valeur in modifications.items():
            setattr(user, champ, valeur)
        apres = {"nom": user.nom, "poste": user.poste, "role": user.role}

        await self.audit.record(
            "user.updated",
            entity="user",
            entity_id=user.id,
            actor=actor,
            metadata={"changements": diff(avant, apres)},
        )
        await self.session.commit()
        return await self.get_user(user.id)

    async def set_status(self, user_id: uuid.UUID, status: UserStatus, actor: User) -> User:
        user = await self.get_user(user_id)
        actif = status is UserStatus.ACTIVE

        if not actif:
            await self._refuser_si_dernier_admin(user, actor, operation="désactiver")

        if user.is_active != actif:
            user.is_active = actif
            if not actif:
                # Désactiver un compte doit couper l'accès **immédiatement** : sans
                # révocation, ses refresh tokens resteraient valides jusqu'à sept
                # jours. La dépendance d'authentification refuse déjà l'access
                # token d'un compte inactif, mais le refresh doit l'être aussi.
                await self._revoke_all_sessions(user)

        await self.audit.record(
            "user.status_changed",
            entity="user",
            entity_id=user.id,
            actor=actor,
            metadata={"status": status.value},
        )
        await self.session.commit()
        return await self.get_user(user.id)

    async def reset_password(
        self, user_id: uuid.UUID, mot_de_passe: str | None, actor: User
    ) -> tuple[User, str | None]:
        """Réinitialisation forcée. Toutes les sessions du compte sont coupées."""
        user = await self.get_user(user_id)

        genere = mot_de_passe is None
        nouveau = mot_de_passe or generate_password()
        user.mot_de_passe_hash = hash_password(nouveau)
        # Un mot de passe réinitialisé l'est souvent parce qu'il a fuité : laisser
        # vivre les sessions ouvertes viderait l'opération de son sens.
        await self._revoke_all_sessions(user)
        user.failed_login_attempts = 0
        user.locked_until = None

        await self.audit.record(
            "user.password_reset",
            entity="user",
            entity_id=user.id,
            actor=actor,
            metadata={"genere_par_le_serveur": genere},
        )
        await self.session.commit()
        return await self.get_user(user.id), (nouveau if genere else None)

    async def unlock(self, user_id: uuid.UUID, actor: User) -> User:
        user = await self.get_user(user_id)
        user.failed_login_attempts = 0
        user.locked_until = None

        await self.audit.record(
            "user.unlocked", entity="user", entity_id=user.id, actor=actor
        )
        await self.session.commit()
        return await self.get_user(user.id)

    # --- Sessions ------------------------------------------------------------

    async def list_sessions(self, user_id: uuid.UUID) -> list[SessionRead]:
        await self.get_user(user_id)
        records = await self.sessions.list_active(user_id, datetime.now(UTC))
        return [SessionRead.model_validate(record) for record in records]

    async def revoke_session(
        self, user_id: uuid.UUID, session_id: uuid.UUID, actor: User
    ) -> None:
        """Coupe une session à distance — tablette perdue ou volée."""
        user = await self.get_user(user_id)
        record = await self.sessions.get_by_id(session_id)
        # Le contrôle d'appartenance évite qu'un identifiant de session deviné ne
        # permette de révoquer la session d'un autre compte.
        if record is None or record.user_id != user.id:
            raise SessionNotFoundError(details={"session_id": str(session_id)})

        maintenant = datetime.now(UTC)
        await self.revoked.revoke(record.jti, user.id, record.expires_at)
        await self.sessions.mark_revoked(record, maintenant)

        await self.audit.record(
            "user.session_revoked",
            entity="user",
            entity_id=user.id,
            actor=actor,
            metadata={"session_id": str(session_id)},
        )
        await self.session.commit()

    async def _revoke_all_sessions(self, user: User) -> None:
        maintenant = datetime.now(UTC)
        for jti, expires_at in await self.sessions.list_active_jtis(user.id, maintenant):
            await self.revoked.revoke(jti, user.id, expires_at)
            await self.sessions.revoke_by_jti(jti, maintenant)

    # --- Garde-fous ----------------------------------------------------------

    async def _refuser_si_dernier_admin(
        self, cible: User, actor: User, *, operation: str
    ) -> None:
        """Empêche de se verrouiller hors du dashboard.

        Deux garde-fous distincts : on ne s'applique pas l'opération à soi-même, et
        on ne retire pas le dernier administrateur actif. Sans eux, il faudrait un
        accès direct à la base pour rouvrir le dashboard.
        """
        if cible.id == actor.id:
            raise SelfModificationError(details={"operation": operation})
        if cible.role is UserRole.ADMIN and cible.is_active:
            restants = await self.users.count_active_admins(excluding=cible.id)
            if restants == 0:
                raise LastAdminError(details={"operation": operation})
