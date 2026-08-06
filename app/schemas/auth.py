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


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class MessageResponse(BaseModel):
    message: str


class ForgotPasswordRequest(BaseModel):
    identifiant: str = Field(min_length=1, max_length=100)
