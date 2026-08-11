from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import UserRole
from app.schemas.common import ORMModel


class UserRead(ORMModel):
    id: uuid.UUID
    nom: str
    identifiant: str
    poste: str | None = None
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime


class LoginRequest(BaseModel):
    identifiant: str = Field(min_length=1, max_length=100)
    mot_de_passe: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Durée de validité de l'access token, en secondes.")
    user: UserRead


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    # Champ **additif** : renseigné uniquement quand le serveur a fait glisser la
    # session. Un client qui l'ignore continue de fonctionner avec son ancien
    # token jusqu'à l'expiration de celui-ci — d'où l'absence de rupture de
    # contrat. Un client qui l'enregistre n'est jamais déconnecté tant qu'il sert.
    refresh_token: str | None = Field(
        default=None,
        description=(
            "Nouveau refresh token à stocker en remplacement du précédent. "
            "Absent tant que la session n'a pas besoin d'être prolongée."
        ),
    )


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class MessageResponse(BaseModel):
    message: str


class ForgotPasswordRequest(BaseModel):
    identifiant: str = Field(min_length=1, max_length=100)
