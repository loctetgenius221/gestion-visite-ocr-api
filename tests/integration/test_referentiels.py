from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.referentiel import Service


class TestServices:
    async def test_liste_des_services(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.get("/services", headers=auth_headers)

        assert response.status_code == 200
        codes = {service["code"] for service in response.json()}
        assert codes == {"DRH", "DSI"}

    async def test_hierarchie_rendue_en_arbre(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        parent: Service = seeded["service"]
        session.add(
            Service(name="Bureau des Carrières", code="DRH-CAR", parent_id=parent.id)
        )
        await session.commit()

        response = await client.get("/services", headers=auth_headers)

        arbre = {service["code"]: service for service in response.json()}
        # L'enfant n'apparaît qu'imbriqué, jamais à la racine.
        assert "DRH-CAR" not in arbre
        assert [child["code"] for child in arbre["DRH"]["children"]] == ["DRH-CAR"]

    async def test_agents_dun_service(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        service_id = seeded["service"].id
        response = await client.get(f"/services/{service_id}/agents", headers=auth_headers)

        assert response.status_code == 200
        assert [agent["name"] for agent in response.json()] == ["Aminata Diallo"]

    async def test_agents_dun_service_inexistant_renvoie_404(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.get(f"/services/{uuid.uuid4()}/agents", headers=auth_headers)

        assert response.status_code == 404
        assert response.json()["error_code"] == "SERVICE_NOT_FOUND"

    async def test_sans_authentification_renvoie_401(self, client: AsyncClient, seeded: dict):
        assert (await client.get("/services")).status_code == 401


class TestAgents:
    async def test_liste_complete(self, client: AsyncClient, seeded: dict, auth_headers: dict):
        response = await client.get("/agents", headers=auth_headers)

        assert response.status_code == 200
        assert {agent["name"] for agent in response.json()} == {"Aminata Diallo", "Mamadou Ba"}

    async def test_filtre_par_service(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.get(
            "/agents", params={"service_id": str(seeded["other_service"].id)}, headers=auth_headers
        )

        assert [agent["name"] for agent in response.json()] == ["Mamadou Ba"]


class TestPurposes:
    async def test_liste_des_motifs(self, client: AsyncClient, seeded: dict, auth_headers: dict):
        response = await client.get("/purposes", headers=auth_headers)

        assert response.status_code == 200
        assert [purpose["libelle"] for purpose in response.json()] == [
            "Rendez-vous professionnel"
        ]
