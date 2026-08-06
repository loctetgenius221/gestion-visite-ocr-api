from __future__ import annotations

from httpx import AsyncClient

from tests.integration.test_visits import visit_payload


class TestDashboardStats:
    async def test_stats_a_vide(self, client: AsyncClient, seeded: dict, auth_headers: dict):
        response = await client.get("/dashboard/stats", headers=auth_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["visites_du_jour"] == 0
        assert body["presents_actuellement"] == 0
        assert body["sorties_du_jour"] == 0
        assert body["duree_moyenne_visite_minutes"] is None

    async def test_stats_apres_une_entree_et_une_sortie(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        premiere = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)
        seconde_payload = visit_payload(seeded)
        seconde_payload["visitor"]["numero_document"] = "999"
        await client.post("/visits", json=seconde_payload, headers=auth_headers)

        await client.put(f"/visits/{premiere.json()['id']}/checkout", headers=auth_headers)

        response = await client.get("/dashboard/stats", headers=auth_headers)

        body = response.json()
        assert body["visites_du_jour"] == 2
        assert body["presents_actuellement"] == 1
        assert body["sorties_du_jour"] == 1
        assert body["visites_semaine"] == 2
        assert body["duree_moyenne_visite_minutes"] is not None

    async def test_sans_authentification_renvoie_401(self, client: AsyncClient, seeded: dict):
        assert (await client.get("/dashboard/stats")).status_code == 401
