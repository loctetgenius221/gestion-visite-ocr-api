"""Durcissement pour le SaaS multi-organisations (ADR-023, ADR-024).

Chaque test reproduit une faille relevée par l'audit de mise en production :
il échouait avant la correction. La structure `voisine` (conftest) n'a aucun
lien avec celle de l'agent de test : tout ce qui la touche franchit une
frontière entre clients.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.core.security import create_refresh_token
from app.core.storage_urls import normalize_storage_url, sign_storage_url
from app.models.refresh_token import RevokedToken
from app.models.user_session import UserSession
from app.repositories.user_repository import RevokedTokenRepository, UserSessionRepository
from tests.conftest import TEST_PASSWORD
from tests.integration.test_visits import visit_payload

PIECE_DE_LA_VOISINE = {
    "prenom": "Khady",
    "nom": "Ndoye",
    "type_document": "CNI",
    "numero_document": "PAD0000001",
}


class TestReferencesDUneAutreOrganisation:
    """1.1 — une visite n'écrit que dans le registre de son organisation."""

    async def test_service_d_une_autre_organisation_refuse(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        payload = visit_payload(seeded, service_id=str(voisine["service"].id), agent_id=None)

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 403
        assert response.json()["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"

    async def test_motif_d_une_autre_organisation_refuse(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        payload = visit_payload(seeded, purpose_id=str(voisine["purpose"].id))

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 403

    async def test_la_synchro_hors_ligne_applique_la_meme_regle(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        etrangere = visit_payload(
            seeded, service_id=str(voisine["service"].id), agent_id=None, client_reference="S-1"
        )
        valide = visit_payload(seeded, client_reference="S-2")
        valide["visitor"]["numero_document"] = "SYNC000002"

        response = await client.post(
            "/visits/sync", json={"visits": [etrangere, valide]}, headers=auth_headers
        )

        resultats = response.json()["results"]
        assert resultats[0]["status"] == "error"
        assert resultats[0]["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"
        # Le reste du lot passe : une ligne fautive ne coûte pas les autres.
        assert resultats[1]["status"] == "created"

    async def test_une_correction_ne_change_pas_la_visite_de_registre(
        self,
        client: AsyncClient,
        seeded: dict,
        auth_headers: dict,
        org_admin_headers: dict,
        voisine: dict,
    ):
        creee = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)

        response = await client.patch(
            f"/visits/{creee.json()['id']}",
            json={
                "reason": "Mauvais service",
                "service_id": str(voisine["service"].id),
                "agent_id": None,
            },
            headers=org_admin_headers,
        )

        assert response.status_code == 403

    async def test_reprise_d_une_fiche_jamais_venue_chez_soi(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        """La fiche existe, mais chez la voisine : répondre comme si elle n'existait pas."""
        payload = visit_payload(seeded)
        del payload["visitor"]
        payload["visitor_id"] = str(voisine["visitor"].id)

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 404
        assert response.json()["error_code"] == "VISITOR_NOT_FOUND"


class TestPresenceParOrganisation:
    """1.2 — deux ministères tiennent deux registres distincts."""

    async def test_present_chez_la_voisine_n_empeche_pas_d_entrer_ici(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        payload = visit_payload(seeded, visitor=dict(PIECE_DE_LA_VOISINE))

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 201, response.text

    async def test_le_doublon_dans_le_meme_registre_reste_refuse(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)

        response = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)

        assert response.status_code == 409
        assert response.json()["error_code"] == "VISITOR_ALREADY_PRESENT"


class TestReferenceClientParOrganisation:
    """1.3 — la clé d'idempotence d'un client ne bloque pas un autre client."""

    async def test_meme_reference_dans_deux_organisations(
        self,
        client: AsyncClient,
        seeded: dict,
        auth_headers: dict,
        voisine: dict,
        voisine_headers: dict,
    ):
        chez_la_voisine = visit_payload(
            voisine,
            client_reference="TABLETTE-1-0001",
            visitor={**PIECE_DE_LA_VOISINE, "numero_document": "PAD0000099"},
        )
        premiere = await client.post("/visits", json=chez_la_voisine, headers=voisine_headers)
        assert premiere.status_code == 201, premiere.text

        ici = visit_payload(seeded, client_reference="TABLETTE-1-0001")
        response = await client.post("/visits", json=ici, headers=auth_headers)

        assert response.status_code == 201, response.text

    async def test_la_reference_reste_idempotente_chez_soi(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        payload = visit_payload(seeded, client_reference="TABLETTE-1-0002")
        await client.post("/visits", json=payload, headers=auth_headers)

        payload["visitor"]["numero_document"] = "AUTRE000001"
        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 409
        assert response.json()["error_code"] == "DUPLICATE_VISIT"


class TestFichiersReferences:
    """1.5 / 1.6 — un payload ne référence que des fichiers du stockage, et les siens."""

    async def test_fichier_d_une_autre_organisation_refuse(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        chemin = f"/storage/uploads/org/{voisine['organization'].id}/signatures/2026/10/x.png"
        payload = visit_payload(seeded, signature_url=chemin)

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 403
        # Le chemin d'un fichier d'autrui n'est pas renvoyé.
        assert chemin not in response.text

    @pytest.mark.parametrize(
        "url",
        [
            "https://ailleurs.example/photo.png",
            "/storage/uploads/../../etc/passwd",
            "/tmp/photo.png",
        ],
    )
    async def test_url_hors_du_stockage_refusee(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, url: str
    ):
        payload = visit_payload(seeded, signature_url=url)

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 400

    async def test_la_signature_renvoyee_par_le_client_est_retiree(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        chemin = f"/storage/uploads/org/{seeded['organization'].id}/signatures/2026/10/s.png"
        payload = visit_payload(
            seeded, signature_url=f"https://api.exemple.sn{chemin}?md5=abc&expires=1"
        )

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 201, response.text
        assert response.json()["signature_url"] == chemin


class TestUrlsSignees:
    """ADR-024 — format attendu par nginx `secure_link_md5`."""

    CLE = "cle-de-signature-de-test-suffisamment-longue"

    def test_signature_compatible_nginx(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "STORAGE_URL_SIGNING_KEY", self.CLE)
        chemin = "/storage/uploads/org/x/signatures/2026/10/s.png"

        signee = sign_storage_url(chemin)

        assert signee is not None
        base, requete = signee.split("?")
        parametres = dict(p.split("=") for p in requete.split("&"))
        attendu = base64.urlsafe_b64encode(
            hashlib.md5(f"{parametres['expires']}{chemin} {self.CLE}".encode()).digest()
        ).rstrip(b"=").decode()
        assert base == chemin
        assert parametres["md5"] == attendu

    def test_les_logos_restent_publics(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "STORAGE_URL_SIGNING_KEY", self.CLE)
        logo = "/storage/uploads/organizations/abc/2026/10/logo.png"

        assert sign_storage_url(logo) == logo

    def test_sans_cle_le_chemin_est_rendu_tel_quel(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "STORAGE_URL_SIGNING_KEY", "")

        assert sign_storage_url("/storage/uploads/mrz/a.png") == "/storage/uploads/mrz/a.png"

    def test_normalisation_aller_retour(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "STORAGE_URL_SIGNING_KEY", self.CLE)
        chemin = "/storage/uploads/org/x/documents/recto/2026/10/r.png"

        assert normalize_storage_url(sign_storage_url(chemin)) == chemin

    async def test_les_lectures_renvoient_des_urls_signees(
        self,
        client: AsyncClient,
        seeded: dict,
        auth_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "STORAGE_URL_SIGNING_KEY", self.CLE)
        chemin = f"/storage/uploads/org/{seeded['organization'].id}/signatures/2026/10/s.png"

        creee = await client.post(
            "/visits", json=visit_payload(seeded, signature_url=chemin), headers=auth_headers
        )

        assert creee.status_code == 201, creee.text
        assert creee.json()["signature_url"].startswith(f"{chemin}?md5=")


async def _connexion(client: AsyncClient) -> str:
    response = await client.post(
        "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["refresh_token"]


class TestRefreshTokenRattacheASaSession:
    """1.4 — révoquer une tablette coupe tous ses tokens, y compris l'ancien."""

    async def test_la_revocation_coupe_aussi_le_token_remplace(
        self,
        client: AsyncClient,
        seeded: dict,
        admin_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        ancien = await _connexion(client)
        monkeypatch.setattr(
            settings, "REFRESH_TOKEN_EXPIRE_DAYS", settings.REFRESH_TOKEN_EXPIRE_DAYS * 4
        )
        nouveau = (
            await client.post("/auth/refresh", json={"refresh_token": ancien})
        ).json()["refresh_token"]
        assert nouveau is not None

        user_id = str(seeded["user"].id)
        sessions = (await client.get(f"/users/{user_id}/sessions", headers=admin_headers)).json()
        await client.delete(
            f"/users/{user_id}/sessions/{sessions[0]['id']}", headers=admin_headers
        )

        for token in (ancien, nouveau):
            response = await client.post("/auth/refresh", json={"refresh_token": token})
            assert response.status_code == 401

    async def test_le_token_remplace_ne_fait_pas_glisser_la_session(
        self, client: AsyncClient, session, seeded: dict, monkeypatch: pytest.MonkeyPatch
    ):
        """Sinon il évincerait le token courant, que le client a peut-être enregistré."""
        ancien = await _connexion(client)
        monkeypatch.setattr(
            settings, "REFRESH_TOKEN_EXPIRE_DAYS", settings.REFRESH_TOKEN_EXPIRE_DAYS * 4
        )
        nouveau = (
            await client.post("/auth/refresh", json={"refresh_token": ancien})
        ).json()["refresh_token"]

        rejoue = await client.post("/auth/refresh", json={"refresh_token": ancien})

        assert rejoue.status_code == 200
        assert rejoue.json()["refresh_token"] is None
        courant = await client.post("/auth/refresh", json={"refresh_token": nouveau})
        assert courant.status_code == 200

    async def test_un_token_sans_session_est_refuse(
        self, client: AsyncClient, session, seeded: dict
    ):
        token, _ = create_refresh_token(str(seeded["user"].id))

        response = await client.post("/auth/refresh", json={"refresh_token": token})

        assert response.status_code == 401
        sessions = (await session.execute(select(UserSession))).scalars().all()
        assert sessions == []


class TestPurgeDesSessions:
    """2.5 — les tables de sessions ne grossissent plus indéfiniment."""

    async def test_seules_les_lignes_mortes_depuis_longtemps_partent(
        self, session, seeded: dict
    ):
        maintenant = datetime.now(UTC)
        user_id = seeded["user"].id
        dans_5_jours = maintenant + timedelta(days=5)
        vivante = UserSession(user_id=user_id, jti="vivante", expires_at=dans_5_jours)
        expiree = UserSession(
            user_id=user_id, jti="expiree", expires_at=maintenant - timedelta(days=40)
        )
        revoquee_hier = UserSession(
            user_id=user_id,
            jti="revoquee-hier",
            expires_at=dans_5_jours,
            revoked_at=maintenant - timedelta(days=1),
        )
        session.add_all([vivante, expiree, revoquee_hier])
        session.add_all(
            [
                RevokedToken(
                    jti="r-expire", user_id=user_id, expires_at=maintenant - timedelta(days=1)
                ),
                RevokedToken(jti="r-vivant", user_id=user_id, expires_at=dans_5_jours),
            ]
        )
        await session.commit()

        seuil = maintenant - timedelta(days=30)
        supprimees = await UserSessionRepository(session).purge(
            expired_before=seuil, revoked_before=seuil
        )
        tokens = await RevokedTokenRepository(session).purge_expired()
        await session.commit()

        assert supprimees == 1
        assert tokens == 1
        restantes = {s.jti for s in (await session.execute(select(UserSession))).scalars()}
        assert restantes == {"vivante", "revoquee-hier"}


class TestRotationDeLaCleJwt:
    """1.12 — changer de clé ne déconnecte pas le parc."""

    async def test_la_cle_precedente_reste_acceptee(
        self, client: AsyncClient, seeded: dict, monkeypatch: pytest.MonkeyPatch
    ):
        refresh_token = await _connexion(client)
        ancienne_cle = settings.JWT_SECRET_KEY
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "nouvelle-cle-de-signature-" + "x" * 40)
        monkeypatch.setattr(settings, "JWT_SECRET_KEY_PREVIOUS", ancienne_cle)

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 200

    async def test_sans_cle_precedente_les_anciens_jetons_tombent(
        self, client: AsyncClient, seeded: dict, monkeypatch: pytest.MonkeyPatch
    ):
        refresh_token = await _connexion(client)
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "nouvelle-cle-de-signature-" + "x" * 40)
        monkeypatch.setattr(settings, "JWT_SECRET_KEY_PREVIOUS", "")

        response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

        assert response.status_code == 401
