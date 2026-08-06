"""Garde-fous de configuration : ce qui doit empêcher un démarrage en production."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import DEV_JWT_SECRET, Settings

SAFE_PRODUCTION = {
    "_env_file": None,
    "ENVIRONMENT": "production",
    "JWT_SECRET_KEY": "k" * 64,
    "CORS_ORIGINS": "https://app.exemple.sn",
    "DATABASE_URL": "postgresql+asyncpg://sigv:motdepasse@db:5432/sigv",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**SAFE_PRODUCTION, **overrides})  # type: ignore[arg-type]


def test_production_accepte_une_configuration_saine() -> None:
    settings = _settings()
    assert settings.is_production


@pytest.mark.parametrize(
    ("overrides", "extrait_attendu"),
    [
        ({"JWT_SECRET_KEY": DEV_JWT_SECRET}, "valeur d'exemple"),
        ({"JWT_SECRET_KEY": "trop-court"}, "32 caractères"),
        ({"CORS_ORIGINS": "*"}, "CORS_ORIGINS"),
        ({"CORS_ORIGINS": "https://app.exemple.sn,*"}, "CORS_ORIGINS"),
        ({"DATABASE_URL": "sqlite+aiosqlite:///./sigv.db"}, "PostgreSQL"),
        ({"DB_ECHO": True}, "DB_ECHO"),
    ],
)
def test_production_refuse_les_reglages_de_developpement(
    overrides: dict[str, object], extrait_attendu: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(**overrides)
    assert extrait_attendu in str(exc_info.value)


def test_hors_production_les_memes_reglages_passent() -> None:
    """Le développement local doit rester utilisable sans cérémonie."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ENVIRONMENT="development",
        JWT_SECRET_KEY=DEV_JWT_SECRET,
        CORS_ORIGINS="*",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    assert not settings.is_production


class TestValeursAutomatiques:
    """`ENABLE_DOCS` / `SERVE_STORAGE` non renseignés suivent l'environnement."""

    def test_documentation_exposee_hors_production(self) -> None:
        settings = Settings(_env_file=None, ENVIRONMENT="development")  # type: ignore[call-arg]
        assert settings.docs_enabled
        assert settings.serve_storage

    def test_documentation_masquee_en_production(self) -> None:
        settings = _settings()
        assert not settings.docs_enabled
        assert not settings.serve_storage

    def test_reglage_explicite_prime_sur_l_environnement(self) -> None:
        settings = _settings(ENABLE_DOCS=True, SERVE_STORAGE=True)
        assert settings.docs_enabled
        assert settings.serve_storage

    @pytest.mark.parametrize("valeur", ["", "   "])
    def test_variable_vide_vaut_automatique(self, valeur: str) -> None:
        """Un `.env` recopié sans être complété ne doit pas casser le démarrage."""
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            ENVIRONMENT="development",
            ENABLE_DOCS=valeur,
            SERVE_STORAGE=valeur,
        )
        assert settings.ENABLE_DOCS is None
        assert settings.docs_enabled


def test_trusted_hosts_liste() -> None:
    settings = _settings(TRUSTED_HOSTS="api.exemple.sn, www.exemple.sn ,")
    assert settings.trusted_hosts_list == ["api.exemple.sn", "www.exemple.sn"]
