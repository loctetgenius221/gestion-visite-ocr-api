"""Sondes de disponibilité et exposition conditionnelle de la documentation."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import create_app


async def _root_client() -> AsyncClient:
    """Client branché à la racine : les sondes vivent hors du préfixe /api/v1."""
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def test_health_repond_sans_authentification() -> None:
    async with await _root_client() as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_verifie_la_base() -> None:
    async with await _root_client() as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_readiness_repond_503_si_la_base_est_injoignable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un load balancer doit pouvoir sortir l'instance de la rotation."""

    class EngineInjoignable:
        def connect(self) -> None:
            raise OSError("connexion refusée")

    # `AsyncEngine.connect` est en lecture seule : c'est l'engine entier qu'on remplace.
    monkeypatch.setattr("app.main.engine", EngineInjoignable())

    async with await _root_client() as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error_code"] == "SERVICE_UNAVAILABLE"


class TestDocumentation:
    async def test_exposee_hors_production(self) -> None:
        async with await _root_client() as client:
            assert (await client.get("/docs")).status_code == 200
            assert (await client.get("/openapi.json")).status_code == 200

    async def test_masquee_en_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")

        async with await _root_client() as client:
            assert (await client.get("/docs")).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 404
            # L'API reste servie : seule la documentation disparaît.
            assert (await client.get("/health")).status_code == 200


class TestHotesDeConfiance:
    async def test_hote_inattendu_rejete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "TRUSTED_HOSTS", "api.exemple.sn")

        async with await _root_client() as client:
            response = await client.get("/health", headers={"Host": "attaquant.example"})

        assert response.status_code == 400

    async def test_hote_declare_accepte(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "TRUSTED_HOSTS", "api.exemple.sn")

        async with await _root_client() as client:
            response = await client.get("/health", headers={"Host": "api.exemple.sn"})

        assert response.status_code == 200
