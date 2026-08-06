from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from app.core.config import settings
from app.core.errors import InvalidTokenError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


class TestHashage:
    def test_le_hash_ne_contient_pas_le_mot_de_passe_en_clair(self):
        hashed = hash_password("MotDePasse123!")
        assert "MotDePasse123!" not in hashed
        assert hashed.startswith("$2")

    def test_verification_dun_mot_de_passe_correct(self):
        assert verify_password("MotDePasse123!", hash_password("MotDePasse123!"))

    def test_verification_dun_mot_de_passe_incorrect(self):
        assert not verify_password("mauvais", hash_password("MotDePasse123!"))

    def test_deux_hash_du_meme_mot_de_passe_different_par_le_sel(self):
        assert hash_password("identique") != hash_password("identique")

    def test_mot_de_passe_tres_long_ne_leve_pas(self):
        # bcrypt refuse au-delà de 72 octets : la troncature doit être gérée en amont.
        long = "a" * 200
        assert verify_password(long, hash_password(long))

    def test_hash_malforme_est_rejete_sans_exception(self):
        assert not verify_password("peu importe", "pas-un-hash-bcrypt")


class TestJwt:
    def test_access_token_porte_le_sujet_et_le_type(self):
        token, payload = create_access_token("user-123")
        decoded = decode_token(token, expected_type="access")

        assert decoded.subject == "user-123"
        assert decoded.token_type == "access"
        assert decoded.jti == payload.jti

    def test_refresh_token_expire_plus_tard_que_laccess_token(self):
        _, access = create_access_token("user-123")
        _, refresh = create_refresh_token("user-123")
        assert refresh.expires_at > access.expires_at

    def test_deux_tokens_ont_des_jti_distincts(self):
        _, premier = create_access_token("user-123")
        _, second = create_access_token("user-123")
        assert premier.jti != second.jti

    def test_type_inattendu_est_rejete(self):
        token, _ = create_refresh_token("user-123")
        with pytest.raises(InvalidTokenError):
            decode_token(token, expected_type="access")

    def test_signature_invalide_est_rejetee(self):
        token = jwt.encode(
            {"sub": "user-123", "type": "access", "jti": "x", "exp": 9999999999},
            "mauvaise-cle",
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(InvalidTokenError):
            decode_token(token)

    def test_token_expire_est_rejete(self):
        expire = datetime.now(UTC) - timedelta(minutes=5)
        token = jwt.encode(
            {
                "sub": "user-123",
                "type": "access",
                "jti": "x",
                "exp": int(expire.timestamp()),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(InvalidTokenError):
            decode_token(token)

    def test_claim_obligatoire_manquant_est_rejete(self):
        token = jwt.encode(
            {"sub": "user-123", "exp": 9999999999},
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(InvalidTokenError):
            decode_token(token)

    def test_texte_arbitraire_est_rejete(self):
        with pytest.raises(InvalidTokenError):
            decode_token("pas-un-jwt")
