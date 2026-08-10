"""Administration des comptes : création, statut, verrouillage, sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from app.models.user import User
from tests.conftest import TEST_PASSWORD


class TestCreation:
    async def test_le_mot_de_passe_genere_nest_renvoye_quune_fois(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        creation = await client.post(
            "/users",
            headers=admin_headers,
            json={"nom": "Awa Ndiaye", "identifiant": "agent042", "role": "AGENT_CONTROLE"},
        )

        assert creation.status_code == 201
        mot_de_passe = creation.json()["mot_de_passe"]
        assert mot_de_passe and len(mot_de_passe) >= 12

        # Relire le compte ne doit plus jamais exposer le mot de passe.
        user_id = creation.json()["user"]["id"]
        detail = await client.get(f"/users/{user_id}", headers=admin_headers)
        assert "mot_de_passe" not in detail.json()
        assert "mot_de_passe_hash" not in detail.json()

        # ...et il fonctionne réellement.
        login = await client.post(
            "/auth/login", json={"identifiant": "agent042", "mot_de_passe": mot_de_passe}
        )
        assert login.status_code == 200

    async def test_un_mot_de_passe_fourni_nest_pas_renvoye(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Rien à retourner : l'administrateur le connaît déjà."""
        response = await client.post(
            "/users",
            headers=admin_headers,
            json={
                "nom": "Cheikh Fall",
                "identifiant": "agent043",
                "role": "AGENT_CONTROLE",
                "mot_de_passe": "MotDePasseSolide2026",
            },
        )

        assert response.status_code == 201
        assert response.json()["mot_de_passe"] is None

    async def test_identifiant_deja_pris(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/users",
            headers=admin_headers,
            json={"nom": "Doublon", "identifiant": "agent001", "role": "AGENT_CONTROLE"},
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "DUPLICATE_IDENTIFIANT"

    async def test_mot_de_passe_trop_court_refuse(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/users",
            headers=admin_headers,
            json={
                "nom": "X",
                "identifiant": "agent044",
                "role": "AGENT_CONTROLE",
                "mot_de_passe": "court",
            },
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"


class TestListeEtFiltres:
    async def test_enveloppe_de_pagination_identique_a_visits(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/users?page=1&page_size=10", headers=admin_headers)

        assert response.status_code == 200
        assert set(response.json()) == {"items", "total", "page", "page_size"}

    async def test_filtre_par_role(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/users?role=ADMIN", headers=admin_headers)

        assert response.status_code == 200
        assert {item["role"] for item in response.json()["items"]} == {"ADMIN"}

    async def test_recherche_par_identifiant(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/users?search=agent0", headers=admin_headers)

        assert response.status_code == 200
        assert all("agent0" in item["identifiant"] for item in response.json()["items"])


class TestStatut:
    async def test_desactiver_coupe_laccces_immediatement(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        """Le refresh token doit mourir avec le compte, pas sept jours plus tard."""
        login = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        refresh_token = login.json()["refresh_token"]
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]

        desactivation = await client.patch(
            f"/users/{user_id}/status", headers=admin_headers, json={"status": "inactive"}
        )
        assert desactivation.status_code == 200
        assert desactivation.json()["status"] == "inactive"
        assert desactivation.json()["is_active"] is False

        refresh = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert refresh.status_code == 401
        assert refresh.json()["error_code"] == "INVALID_TOKEN"

    async def test_un_admin_ne_peut_pas_se_desactiver(
        self, client: AsyncClient, admin_headers: dict[str, str], admin: User
    ) -> None:
        """Sans ce garde-fou, on se verrouille hors du dashboard."""
        response = await client.patch(
            f"/users/{admin.id}/status", headers=admin_headers, json={"status": "inactive"}
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "SELF_MODIFICATION_FORBIDDEN"

    async def test_le_dernier_admin_ne_peut_pas_etre_retrograde(
        self, client: AsyncClient, admin_headers: dict[str, str], session
    ) -> None:
        second = await client.post(
            "/users",
            headers=admin_headers,
            json={"nom": "Second admin", "identifiant": "admin002", "role": "ADMIN"},
        )
        second_id = second.json()["user"]["id"]

        # Deux admins actifs : la rétrogradation du second passe.
        premiere = await client.put(
            f"/users/{second_id}", headers=admin_headers, json={"role": "AGENT_CONTROLE"}
        )
        assert premiere.status_code == 200

        # Il ne reste que l'admin courant, qui ne peut pas se rétrograder lui-même.
        moi = (await client.get("/me", headers=admin_headers)).json()
        seconde = await client.put(
            f"/users/{moi['id']}", headers=admin_headers, json={"role": "AGENT_CONTROLE"}
        )
        assert seconde.status_code == 409


class TestVerrouillage:
    async def test_le_compte_se_verrouille_apres_cinq_echecs(
        self, client: AsyncClient, session, seeded: dict
    ) -> None:
        for _ in range(5):
            reponse = await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "faux"}
            )
            assert reponse.status_code == 401

        # Le bon mot de passe ne suffit plus : le compte est verrouillé.
        response = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "LOCKED_ACCOUNT"

    async def test_un_compte_inconnu_nincremente_rien_et_reste_indiscernable(
        self, client: AsyncClient, seeded: dict
    ) -> None:
        """Anti-énumération : même code d'erreur qu'un mot de passe faux."""
        inconnu = await client.post(
            "/auth/login", json={"identifiant": "inexistant999", "mot_de_passe": "x" * 12}
        )
        connu = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "faux"}
        )

        assert inconnu.status_code == connu.status_code == 401
        assert inconnu.json()["error_code"] == connu.json()["error_code"] == "INVALID_CREDENTIALS"

    async def test_le_deblocage_admin_reouvre_le_compte(
        self, client: AsyncClient, admin_headers: dict[str, str], session, seeded: dict
    ) -> None:
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]
        for _ in range(5):
            await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "faux"}
            )

        deblocage = await client.post(f"/users/{user_id}/unlock", headers=admin_headers)
        assert deblocage.status_code == 200
        assert deblocage.json()["failed_login_attempts"] == 0
        assert deblocage.json()["locked_until"] is None

        response = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        assert response.status_code == 200

    async def test_une_connexion_reussie_remet_le_compteur_a_zero(
        self, client: AsyncClient, admin_headers: dict[str, str], session, seeded: dict
    ) -> None:
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]
        for _ in range(3):
            await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "faux"}
            )

        await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )

        detail = await client.get(f"/users/{user_id}", headers=admin_headers)
        assert detail.json()["failed_login_attempts"] == 0
        assert detail.json()["last_login_at"] is not None

    async def test_le_verrou_expire_de_lui_meme(
        self, client: AsyncClient, session, seeded: dict
    ) -> None:
        user = seeded["user"]
        for _ in range(5):
            await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "faux"}
            )

        # On avance dans le temps plutôt que d'attendre le quart d'heure réel.
        courant = (await session.execute(select(User).where(User.id == user.id))).scalar_one()  # type: ignore[union-attr]
        courant.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        response = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        assert response.status_code == 200


class TestSessions:
    async def test_chaque_connexion_ouvre_une_session_listable(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]
        for _ in range(2):
            await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
            )

        response = await client.get(f"/users/{user_id}/sessions", headers=admin_headers)

        assert response.status_code == 200
        assert len(response.json()) == 2
        # Le token lui-même n'est jamais stocké, seul son identifiant l'est.
        assert all("jti" in item and "refresh_token" not in item for item in response.json())

    async def test_revoquer_une_session_coupe_ce_refresh_token_seulement(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        """Cas d'usage : tablette perdue. Les autres appareils restent connectés."""
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]
        premiere = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        seconde = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )

        sessions = (await client.get(f"/users/{user_id}/sessions", headers=admin_headers)).json()
        # Tri antéchronologique : la première session est la plus récente.
        a_revoquer = sessions[0]["id"]

        suppression = await client.delete(
            f"/users/{user_id}/sessions/{a_revoquer}", headers=admin_headers
        )
        assert suppression.status_code == 204

        restantes = (await client.get(f"/users/{user_id}/sessions", headers=admin_headers)).json()
        assert len(restantes) == 1

        # L'un des deux refresh tokens est mort, l'autre vit toujours.
        resultats = [
            (
                await client.post(
                    "/auth/refresh", json={"refresh_token": reponse.json()["refresh_token"]}
                )
            ).status_code
            for reponse in (premiere, seconde)
        ]
        assert sorted(resultats) == [200, 401]


class TestReinitialisationMotDePasse:
    async def test_reinitialiser_coupe_les_sessions_existantes(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        user_id = str(seeded["user"].id)  # type: ignore[union-attr]
        login = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        ancien_refresh = login.json()["refresh_token"]

        reset = await client.post(
            f"/users/{user_id}/reset-password", headers=admin_headers, json={}
        )
        assert reset.status_code == 200
        nouveau = reset.json()["mot_de_passe"]
        assert nouveau

        assert (
            await client.post("/auth/refresh", json={"refresh_token": ancien_refresh})
        ).status_code == 401
        assert (
            await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
            )
        ).status_code == 401
        assert (
            await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": nouveau}
            )
        ).status_code == 200
