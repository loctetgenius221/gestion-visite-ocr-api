from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.main import create_app
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.usefixtures("seeded")


class TestLogin:
    async def test_login_valide_retourne_les_deux_tokens_et_le_profil(self, client: AsyncClient):
        response = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"] and body["refresh_token"]
        assert body["user"]["identifiant"] == "agent001"
        assert body["user"]["role"] == "AGENT_CONTROLE"
        # Le hash du mot de passe ne doit jamais transiter (DoD).
        assert "mot_de_passe_hash" not in body["user"]

    async def test_mauvais_mot_de_passe_renvoie_401_au_format_standard(self, client: AsyncClient):
        response = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "mauvais"}
        )

        assert response.status_code == 401
        body = response.json()
        assert body["error_code"] == "INVALID_CREDENTIALS"
        assert set(body) == {"error_code", "message", "details"}

    async def test_compte_inconnu_renvoie_la_meme_erreur_quun_mot_de_passe_faux(
        self, client: AsyncClient
    ):
        response = await client.post(
            "/auth/login", json={"identifiant": "inconnu", "mot_de_passe": TEST_PASSWORD}
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_CREDENTIALS"

    async def test_payload_incomplet_renvoie_400(self, client: AsyncClient):
        response = await client.post("/auth/login", json={"identifiant": "agent001"})
        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"


class TestLoginOAuth2:
    """`/auth/token` : variante formulaire, utilisée par le bouton Authorize de Swagger."""

    async def test_le_formulaire_oauth2_delivre_un_token_exploitable(self, client: AsyncClient):
        response = await client.post(
            "/auth/token", data={"username": "agent001", "password": TEST_PASSWORD}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["identifiant"] == "agent001"

        # Le token obtenu doit ouvrir les routes protégées.
        me = await client.get("/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200

    async def test_le_token_url_declare_existe_et_accepte_un_formulaire(self):
        """Garde-fou : un `tokenUrl` désignant `/auth/login` (JSON) casserait Swagger."""
        spec = create_app().openapi()

        flow = spec["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"]
        token_url = flow["tokenUrl"]

        # L'URL annoncée doit exister dans le schéma...
        assert token_url in spec["paths"], f"tokenUrl {token_url} ne correspond à aucune route"
        # ...et y accepter un formulaire, pas du JSON.
        contenu = spec["paths"][token_url]["post"]["requestBody"]["content"]
        assert "application/x-www-form-urlencoded" in contenu

    async def test_mauvais_mot_de_passe_renvoie_401(self, client: AsyncClient):
        response = await client.post(
            "/auth/token", data={"username": "agent001", "password": "faux"}
        )

        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_CREDENTIALS"

    async def test_le_json_reste_disponible_pour_lapp_mobile(self, client: AsyncClient):
        """Les deux points d'entrée coexistent : JSON pour Flutter, formulaire pour Swagger."""
        json_reponse = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        form_reponse = await client.post(
            "/auth/token", data={"username": "agent001", "password": TEST_PASSWORD}
        )

        assert json_reponse.status_code == form_reponse.status_code == 200
        assert json_reponse.json()["user"] == form_reponse.json()["user"]


class TestMe:
    async def test_profil_accessible_avec_un_token_valide(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ):
        response = await client.get("/me", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["identifiant"] == "agent001"

    async def test_sans_token_renvoie_401(self, client: AsyncClient):
        response = await client.get("/me")
        assert response.status_code == 401
        assert response.json()["error_code"] == "UNAUTHORIZED"

    async def test_token_invalide_renvoie_401(self, client: AsyncClient):
        response = await client.get("/me", headers={"Authorization": "Bearer pas-un-jwt"})
        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_TOKEN"

    async def test_refresh_token_refuse_comme_access_token(self, client: AsyncClient):
        login = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        refresh_token = login.json()["refresh_token"]

        response = await client.get("/me", headers={"Authorization": f"Bearer {refresh_token}"})

        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_TOKEN"


class TestRefreshEtLogout:
    async def test_refresh_delivre_un_nouvel_access_token(self, client: AsyncClient):
        login = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        refresh_token = login.json()["refresh_token"]

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 200
        nouveau = response.json()["access_token"]
        assert nouveau
        # Le nouvel access token doit être exploitable.
        me = await client.get("/me", headers={"Authorization": f"Bearer {nouveau}"})
        assert me.status_code == 200

    async def test_logout_revoque_le_refresh_token(self, client: AsyncClient):
        login = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        refresh_token = login.json()["refresh_token"]

        logout = await client.post("/auth/logout", json={"refresh_token": refresh_token})
        assert logout.status_code == 200

        rejoue = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert rejoue.status_code == 401
        assert rejoue.json()["error_code"] == "INVALID_TOKEN"

    async def test_forgot_password_repond_202_sans_reveler_lexistence_du_compte(
        self, client: AsyncClient
    ):
        connu = await client.post("/auth/forgot-password", json={"identifiant": "agent001"})
        inconnu = await client.post("/auth/forgot-password", json={"identifiant": "inconnu"})

        assert connu.status_code == inconnu.status_code == 202
        assert connu.json() == inconnu.json()
