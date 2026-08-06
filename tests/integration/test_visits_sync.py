"""Tests de la synchronisation batch des visites créées hors-ligne (spec §5.3)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.integration.test_visits import visit_payload


def offline_payload(seeded: dict, reference: str, numero: str) -> dict:
    payload = visit_payload(seeded)
    payload["client_reference"] = reference
    payload["visitor"]["numero_document"] = numero
    return payload


class TestSync:
    async def test_batch_valide_est_insere_integralement(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        body = {
            "visits": [
                offline_payload(seeded, "offline-1", "111"),
                offline_payload(seeded, "offline-2", "222"),
            ]
        }

        response = await client.post("/visits/sync", json=body, headers=auth_headers)

        assert response.status_code == 200, response.text
        result = response.json()
        compte = (result["total"], result["created"], result["conflicts"], result["errors"])
        assert compte == (2, 2, 0, 0)
        assert all(item["status"] == "created" for item in result["results"])
        assert all(item["visit_id"] for item in result["results"])

    async def test_rejeu_du_meme_batch_est_signale_en_conflit_sans_doublon(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        body = {"visits": [offline_payload(seeded, "offline-1", "111")]}
        await client.post("/visits/sync", json=body, headers=auth_headers)

        rejeu = await client.post("/visits/sync", json=body, headers=auth_headers)

        assert rejeu.status_code == 200
        result = rejeu.json()
        assert result["conflicts"] == 1
        assert result["results"][0]["status"] == "conflict"
        assert result["results"][0]["error_code"] == "DUPLICATE_VISIT"

        listing = await client.get("/visits", headers=auth_headers)
        assert listing.json()["total"] == 1

    async def test_un_item_invalide_ne_fait_pas_perdre_les_autres(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        invalide = offline_payload(seeded, "offline-ko", "999")
        invalide["service_id"] = str(uuid.uuid4())
        body = {
            "visits": [
                offline_payload(seeded, "offline-1", "111"),
                invalide,
                offline_payload(seeded, "offline-3", "333"),
            ]
        }

        response = await client.post("/visits/sync", json=body, headers=auth_headers)

        result = response.json()
        assert (result["created"], result["errors"]) == (2, 1)
        assert [item["status"] for item in result["results"]] == ["created", "error", "created"]
        assert result["results"][1]["error_code"] == "SERVICE_NOT_FOUND"

        listing = await client.get("/visits", headers=auth_headers)
        assert listing.json()["total"] == 2

    async def test_index_et_reference_client_sont_renvoyes_pour_le_rapprochement(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        body = {"visits": [offline_payload(seeded, "ref-abc", "111")]}

        response = await client.post("/visits/sync", json=body, headers=auth_headers)

        item = response.json()["results"][0]
        assert item["index"] == 0
        assert item["client_reference"] == "ref-abc"

    async def test_batch_vide_est_rejete(
        self, client: AsyncClient, auth_headers: dict, seeded: dict
    ):
        response = await client.post("/visits/sync", json={"visits": []}, headers=auth_headers)
        assert response.status_code == 400

    async def test_sans_authentification_renvoie_401(self, client: AsyncClient, seeded: dict):
        body = {"visits": [offline_payload(seeded, "offline-1", "111")]}
        response = await client.post("/visits/sync", json=body)
        assert response.status_code == 401
