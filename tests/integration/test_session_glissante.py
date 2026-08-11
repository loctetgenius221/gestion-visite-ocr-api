"""Durée de session : l'agent du poste d'accueil ne doit pas se reconnecter sans cesse.

Le poste tourne toute la journée et les visites s'enchaînent. La session glisse
donc tant que l'appareil sert, et n'expire que s'il cesse d'être utilisé — ou sur
une déconnexion explicite.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.user_session import UserSession
from tests.conftest import TEST_PASSWORD


async def _connexion(client: AsyncClient) -> str:
    response = await client.post(
        "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["refresh_token"]


def _simuler_une_session_en_fin_de_vie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Place le token déjà émis au-delà de la moitié de sa vie.

    Le seuil se calcule sur l'`exp` inscrit **dans le JWT**, immuable une fois
    signé : vieillir la ligne en base ne changerait rien. On allonge donc la durée
    configurée après l'émission, ce qui revient exactement au même du point de vue
    du serveur — un token dont il reste moins de la moitié à vivre — sans avoir à
    manipuler l'horloge.
    """
    monkeypatch.setattr(
        settings, "REFRESH_TOKEN_EXPIRE_DAYS", settings.REFRESH_TOKEN_EXPIRE_DAYS * 4
    )


class TestDureeParDefaut:
    """Porte sur les valeurs **par défaut du code**, pas sur celles du `.env` local.

    Un `.env` de développement peut légitimement les surcharger ; ce qui doit être
    verrouillé, c'est ce que l'application applique en l'absence de configuration.
    """

    def test_un_mois_de_session(self) -> None:
        """Une reconnexion mensuelle, pas hebdomadaire."""
        assert Settings.model_fields["REFRESH_TOKEN_EXPIRE_DAYS"].default == 30

    def test_le_glissement_est_actif_par_defaut(self) -> None:
        assert Settings.model_fields["REFRESH_TOKEN_SLIDING"].default is True

    def test_laccess_token_reste_court(self) -> None:
        """C'est lui qui accompagne chaque requête, donc lui qui fuite en premier.

        Sa brièveté est invisible pour l'agent tant que le client rafraîchit.
        """
        assert Settings.model_fields["ACCESS_TOKEN_EXPIRE_MINUTES"].default <= 60


class TestGlissement:
    async def test_pas_de_nouveau_token_en_debut_de_vie(
        self, client: AsyncClient, seeded: dict
    ) -> None:
        """Le cas courant : rien à prolonger, rien à renvoyer."""
        refresh_token = await _connexion(client)

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 200
        assert response.json()["refresh_token"] is None
        assert response.json()["access_token"]

    async def test_un_token_a_mi_vie_est_renouvele(
        self, client: AsyncClient, seeded: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refresh_token = await _connexion(client)
        _simuler_une_session_en_fin_de_vie(monkeypatch)

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        nouveau = response.json()["refresh_token"]
        assert nouveau is not None
        assert nouveau != refresh_token

        # Le nouveau token ouvre bien une session complète.
        suite = await client.post("/auth/refresh", json={"refresh_token": nouveau})
        assert suite.status_code == 200

    async def test_lancien_token_reste_valide_apres_glissement(
        self, client: AsyncClient, seeded: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rétro-compatibilité : un client qui ignore le champ ne casse pas."""
        refresh_token = await _connexion(client)
        _simuler_une_session_en_fin_de_vie(monkeypatch)

        await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        ancien = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert ancien.status_code == 200

    async def test_le_glissement_ne_multiplie_pas_les_sessions(
        self, client: AsyncClient, session, seeded: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Une ligne par appareil : la liste du dashboard doit rester lisible."""
        refresh_token = await _connexion(client)
        _simuler_une_session_en_fin_de_vie(monkeypatch)

        await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        total = len((await session.execute(select(UserSession))).scalars().all())
        assert total == 1

    async def test_glissement_desactivable(
        self, client: AsyncClient, session, seeded: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "REFRESH_TOKEN_SLIDING", False)
        refresh_token = await _connexion(client)
        _simuler_une_session_en_fin_de_vie(monkeypatch)

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 200
        assert response.json()["refresh_token"] is None


class TestCeQuiCoupeLaSessionMalgreLeGlissement:
    """Le glissement ne doit jamais rendre une session irrévocable."""

    async def test_la_deconnexion_volontaire(
        self, client: AsyncClient, seeded: dict
    ) -> None:
        refresh_token = await _connexion(client)

        await client.post("/auth/logout", json={"refresh_token": refresh_token})

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_TOKEN"

    async def test_la_revocation_a_distance_par_un_admin(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        """Le cas de la tablette perdue : la session longue doit pouvoir être coupée."""
        refresh_token = await _connexion(client)
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]

        sessions = (await client.get(f"/users/{user_id}/sessions", headers=admin_headers)).json()
        await client.delete(
            f"/users/{user_id}/sessions/{sessions[0]['id']}", headers=admin_headers
        )

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert response.status_code == 401

    async def test_la_desactivation_du_compte(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        refresh_token = await _connexion(client)
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]

        await client.patch(
            f"/users/{user_id}/status", headers=admin_headers, json={"status": "inactive"}
        )

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert response.status_code == 401

    async def test_la_reinitialisation_du_mot_de_passe(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        refresh_token = await _connexion(client)
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]

        await client.post(f"/users/{user_id}/reset-password", headers=admin_headers, json={})

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert response.status_code == 401
