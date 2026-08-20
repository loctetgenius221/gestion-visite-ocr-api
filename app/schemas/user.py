"""Schémas de l'administration des comptes (dashboard web, rôle ADMIN)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import UserRole, UserStatus
from app.schemas.common import ORMModel

# Plancher volontairement bas, ramené de 12 à 6 : l'application est interne, et le
# mot de passe est retapé sur une tablette à chaque prise de poste. La longueur se
# paie alors en contournements — mot de passe noté à côté du poste, ou partagé.
#
# Ce qui tient l'attaque **en ligne** n'est de toute façon pas la longueur mais le
# verrouillage : `max_failed_login_attempts` (5 par défaut) bloque le compte
# pendant `account_lockout_minutes`. Le compromis porte sur l'attaque **hors
# ligne** : si la base des hash fuite, six caractères ne tiendront pas longtemps,
# bcrypt ou non. Assumé ici, à rediscuter si l'API s'ouvre au-delà du ministère.
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128


class UserAdminRead(ORMModel):
    """Vue administrateur d'un compte.

    Distincte de `UserRead`, renvoyée à l'app mobile : elle expose l'état de
    verrouillage et la dernière connexion, qui n'ont pas à circuler côté agent.
    Le hash du mot de passe n'apparaît dans aucune des deux — c'est précisément ce
    que garantit la séparation entre modèle SQLAlchemy et schéma Pydantic.
    """

    id: uuid.UUID
    nom: str
    identifiant: str
    poste: str | None = None
    role: UserRole
    is_active: bool
    failed_login_attempts: int
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> UserStatus:
        """Doublon assumé de `is_active` : le dashboard raisonne en statut nommé."""
        return UserStatus.ACTIVE if self.is_active else UserStatus.INACTIVE


class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    nom: str = Field(min_length=1, max_length=150)
    identifiant: str = Field(min_length=3, max_length=100)
    role: UserRole = UserRole.AGENT_CONTROLE
    poste: str | None = Field(default=None, max_length=150)
    # Omis, un mot de passe est généré par le serveur et renvoyé **une seule fois**
    # dans la réponse de création.
    mot_de_passe: str | None = Field(
        default=None, min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


class UserUpdate(BaseModel):
    """Modification du nom, du poste ou du rôle. L'identifiant n'est pas modifiable.

    Le changer casserait le rattachement mental des visites déjà enregistrées à
    leur auteur ; créer un nouveau compte et désactiver l'ancien est plus honnête.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    nom: str | None = Field(default=None, min_length=1, max_length=150)
    poste: str | None = Field(default=None, max_length=150)
    role: UserRole | None = None


class UserStatusUpdate(BaseModel):
    status: UserStatus


class PasswordResetRequest(BaseModel):
    """Réinitialisation forcée par un administrateur.

    Sans `mot_de_passe`, le serveur en génère un et le renvoie une seule fois.
    """

    mot_de_passe: str | None = Field(
        default=None, min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


class UserCredentialsResponse(BaseModel):
    """Compte accompagné du mot de passe en clair — l'unique occasion de le lire.

    Il n'est jamais stocké ni journalisé : seul son hash bcrypt part en base, et
    `mot_de_passe` fait partie des clés masquées par le formateur de logs.
    """

    user: UserAdminRead
    mot_de_passe: str | None = Field(
        default=None,
        description=(
            "Renseigné uniquement lorsque le mot de passe a été généré par le "
            "serveur. À transmettre à l'utilisateur par un canal sûr."
        ),
    )


class SessionRead(ORMModel):
    """Session ouverte : un refresh token émis, ni révoqué ni expiré."""

    id: uuid.UUID
    jti: str = Field(
        description="Identifiant du refresh token. Le token lui-même n'est pas stocké."
    )
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    user_agent: str | None = None
    ip_address: str | None = None


class UserFilters(BaseModel):
    role: UserRole | None = None
    status: UserStatus | None = None
    search: str | None = Field(default=None, max_length=100, description="Nom ou identifiant.")
    sort: Literal["asc", "desc"] = "desc"
