from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import InvalidTokenError

TokenType = Literal["access", "refresh"]

# bcrypt tronque silencieusement au-delà de 72 octets : on borne explicitement
# pour éviter qu'un mot de passe très long ne lève une erreur côté backend bcrypt.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    """Hash bcrypt du mot de passe.

    NB : on utilise `bcrypt` directement plutôt que `passlib` — passlib 1.7.4 est
    incompatible avec bcrypt >= 4.1 (détection de backend cassée). Voir ADR-003.
    """
    payload = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(payload, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """Contenu utile d'un JWT décodé."""

    subject: str
    token_type: TokenType
    jti: str
    expires_at: datetime


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, TokenPayload]:
    now = datetime.now(UTC)
    expires_at = now + expires_delta
    jti = uuid.uuid4().hex
    claims: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if extra_claims:
        claims.update(extra_claims)
    token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, TokenPayload(
        subject=subject, token_type=token_type, jti=jti, expires_at=expires_at
    )


def create_access_token(
    subject: str, extra_claims: dict[str, Any] | None = None
) -> tuple[str, TokenPayload]:
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims,
    )


def create_refresh_token(
    subject: str, extra_claims: dict[str, Any] | None = None
) -> tuple[str, TokenPayload]:
    return _create_token(
        subject,
        "refresh",
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        extra_claims,
    )


def decode_token(token: str, expected_type: TokenType | None = None) -> TokenPayload:
    """Décode et valide un JWT. Lève `InvalidTokenError` sur signature/expiration/type invalide."""
    try:
        claims = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise InvalidTokenError() from exc

    subject = claims.get("sub")
    token_type = claims.get("type")
    jti = claims.get("jti")
    exp = claims.get("exp")
    if not subject or not token_type or not jti or not exp:
        raise InvalidTokenError()
    if expected_type is not None and token_type != expected_type:
        raise InvalidTokenError(f"Token de type '{expected_type}' attendu.")

    return TokenPayload(
        subject=str(subject),
        token_type=token_type,
        jti=str(jti),
        expires_at=datetime.fromtimestamp(int(exp), tz=UTC),
    )
