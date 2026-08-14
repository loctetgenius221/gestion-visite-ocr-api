from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient


def visit_payload(seeded: dict, **overrides) -> dict:
    """Payload minimal valide de création de visite."""
    payload = {
        "visitor": {
            "prenom": "Aminata",
            "nom": "Diop",
            "type_document": "CNI",
            "numero_document": "1234567890123",
            "nationalite": "SEN",
            "date_naissance": "1990-05-14",
            "sexe": "F",
            "telephone": "+221770000000",
        },
        "service_id": str(seeded["service"].id),
        "agent_id": str(seeded["agent"].id),
        "purpose_id": str(seeded["purpose"].id),
        "badge_number": "B-042",
    }
    payload.update(overrides)
    return payload


class TestCreateVisit:
    async def test_creation_complete_retourne_201_et_le_detail(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.post(
            "/visits", json=visit_payload(seeded), headers=auth_headers
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["statut"] == "PRESENT"
        assert body["visitor"]["nom"] == "Diop"
        assert body["service"]["code"] == "DRH"
        assert body["agent"]["name"] == "Aminata Diallo"
        assert body["purpose"]["libelle"] == "Rendez-vous professionnel"
        assert body["checked_in_user"]["identifiant"] == "agent001"
        assert body["checked_out_at"] is None

    async def test_motif_libre_accepte_sans_purpose_id(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        payload = visit_payload(seeded, purpose_id=None, motif_libre="Dépôt de pli urgent")
        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 201
        assert response.json()["motif_libre"] == "Dépôt de pli urgent"
        assert response.json()["purpose"] is None

    async def test_absence_totale_de_motif_est_rejetee(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        payload = visit_payload(seeded, purpose_id=None)
        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_service_inexistant_renvoie_404(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        payload = visit_payload(seeded, service_id=str(uuid.uuid4()))
        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 404
        assert response.json()["error_code"] == "SERVICE_NOT_FOUND"

    async def test_agent_dun_autre_service_est_refuse(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        payload = visit_payload(seeded, agent_id=str(seeded["other_agent"].id))
        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 409
        assert response.json()["error_code"] == "AGENT_SERVICE_MISMATCH"

    async def test_visiteur_deja_connu_est_reutilise_et_non_duplique(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        premier = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)
        payload = visit_payload(seeded)
        payload["visitor"]["telephone"] = "+221781111111"
        second = await client.post("/visits", json=payload, headers=auth_headers)

        assert second.status_code == 201
        assert premier.json()["visitor"]["id"] == second.json()["visitor"]["id"]
        # Les coordonnées les plus récentes écrasent les anciennes.
        assert second.json()["visitor"]["telephone"] == "+221781111111"

    async def test_nin_saisi_manuellement_est_conserve(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Le NIN n'étant pas dans le MRZ (ADR-005), il arrive par saisie de l'agent."""
        payload = visit_payload(seeded)
        payload["visitor"]["numero_document"] = "10120010718000254"
        payload["visitor"]["nin"] = "1990201700669"

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 201
        visiteur = response.json()["visitor"]
        assert visiteur["numero_document"] == "10120010718000254"
        assert visiteur["nin"] == "1990201700669"

    async def test_nin_saisi_par_blocs_est_normalise(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Le NIN est imprimé par blocs et l'agent le recopie tel quel (ADR-016).

        Sans normalisation, la saisie manuelle et la sortie OCR produiraient deux
        valeurs distinctes pour la même personne, dans une colonne indexée.
        """
        payload = visit_payload(seeded)
        payload["visitor"]["nin"] = "2 k05 2012 00108"

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 201
        assert response.json()["visitor"]["nin"] == "2K05201200108"

    async def test_nin_absent_reste_nul(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)

        assert response.status_code == 201
        assert response.json()["visitor"]["nin"] is None

    async def test_sans_authentification_renvoie_401(self, client: AsyncClient, seeded: dict):
        response = await client.post("/visits", json=visit_payload(seeded))
        assert response.status_code == 401


class TestGetVisit:
    async def test_detail_dune_visite_existante(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        created = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)
        visit_id = created.json()["id"]

        response = await client.get(f"/visits/{visit_id}", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["id"] == visit_id

    async def test_visite_introuvable_renvoie_404_au_format_standard(
        self, client: AsyncClient, auth_headers: dict
    ):
        response = await client.get(f"/visits/{uuid.uuid4()}", headers=auth_headers)

        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == "VISIT_NOT_FOUND"
        assert set(body) == {"error_code", "message", "details"}


class TestCheckout:
    async def test_checkout_passe_la_visite_en_sorti(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        created = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)
        visit_id = created.json()["id"]

        response = await client.put(f"/visits/{visit_id}/checkout", headers=auth_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["statut"] == "SORTI"
        assert body["checked_out_at"] is not None
        assert body["checked_out_user"]["identifiant"] == "agent001"

    async def test_double_checkout_renvoie_409(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        created = await client.post("/visits", json=visit_payload(seeded), headers=auth_headers)
        visit_id = created.json()["id"]
        await client.put(f"/visits/{visit_id}/checkout", headers=auth_headers)

        response = await client.put(f"/visits/{visit_id}/checkout", headers=auth_headers)

        assert response.status_code == 409
        assert response.json()["error_code"] == "VISIT_ALREADY_CLOSED"

    async def test_checkout_dune_visite_inexistante_renvoie_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        response = await client.put(f"/visits/{uuid.uuid4()}/checkout", headers=auth_headers)
        assert response.status_code == 404


class TestListVisits:
    @pytest.fixture
    async def trois_visites(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ) -> list[str]:
        """Trois visites échelonnées dans le temps, dont une clôturée."""
        base = datetime.now(UTC) - timedelta(days=2)
        ids: list[str] = []
        for index, (prenom, nom, numero) in enumerate(
            [("Aminata", "Diop", "111"), ("Moussa", "Fall", "222"), ("Awa", "Ndiaye", "333")]
        ):
            payload = visit_payload(seeded)
            payload["visitor"].update(prenom=prenom, nom=nom, numero_document=numero)
            payload["checked_in_at"] = (base + timedelta(hours=index)).isoformat()
            created = await client.post("/visits", json=payload, headers=auth_headers)
            assert created.status_code == 201, created.text
            ids.append(created.json()["id"])

        await client.put(f"/visits/{ids[0]}/checkout", headers=auth_headers)
        return ids

    async def test_listing_est_pagine_et_enveloppe(
        self, client: AsyncClient, trois_visites: list[str], auth_headers: dict
    ):
        response = await client.get("/visits?page=1&page_size=2", headers=auth_headers)

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"items", "total", "page", "page_size"}
        assert body["total"] == 3
        assert body["page_size"] == 2
        assert len(body["items"]) == 2

    async def test_seconde_page(
        self, client: AsyncClient, trois_visites: list[str], auth_headers: dict
    ):
        response = await client.get("/visits?page=2&page_size=2", headers=auth_headers)
        assert len(response.json()["items"]) == 1

    async def test_filtre_par_statut(
        self, client: AsyncClient, trois_visites: list[str], auth_headers: dict
    ):
        sortis = await client.get("/visits?statut=SORTI", headers=auth_headers)
        presents = await client.get("/visits?statut=PRESENT", headers=auth_headers)

        assert sortis.json()["total"] == 1
        assert presents.json()["total"] == 2

    async def test_recherche_par_nom(
        self, client: AsyncClient, trois_visites: list[str], auth_headers: dict
    ):
        response = await client.get("/visits?search=ndiaye", headers=auth_headers)

        assert response.json()["total"] == 1
        assert response.json()["items"][0]["visitor"]["nom"] == "Ndiaye"

    async def test_recherche_par_numero_de_document(
        self, client: AsyncClient, trois_visites: list[str], auth_headers: dict
    ):
        response = await client.get("/visits?search=222", headers=auth_headers)
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["visitor"]["prenom"] == "Moussa"

    async def test_filtre_par_fenetre_de_dates(
        self, client: AsyncClient, trois_visites: list[str], auth_headers: dict
    ):
        # `params=` plutôt qu'une f-string : le `+00:00` du fuseau doit être encodé.
        hier = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        avant_hier = (datetime.now(UTC) - timedelta(days=3)).isoformat()

        recentes = await client.get(
            "/visits", params={"date_from": hier}, headers=auth_headers
        )
        toutes = await client.get(
            "/visits", params={"date_from": avant_hier, "date_to": hier}, headers=auth_headers
        )

        # Les trois visites datent d'il y a deux jours : aucune n'est « récente ».
        assert recentes.json()["total"] == 0
        assert toutes.json()["total"] == 3

    async def test_tri_ascendant_et_descendant(
        self, client: AsyncClient, trois_visites: list[str], auth_headers: dict
    ):
        asc = await client.get("/visits?sort=asc", headers=auth_headers)
        desc = await client.get("/visits?sort=desc", headers=auth_headers)

        ids_asc = [item["id"] for item in asc.json()["items"]]
        ids_desc = [item["id"] for item in desc.json()["items"]]
        assert ids_asc == list(reversed(ids_desc))
        assert ids_asc[0] == trois_visites[0]

    async def test_page_size_hors_bornes_est_rejete(
        self, client: AsyncClient, auth_headers: dict, seeded: dict
    ):
        response = await client.get("/visits?page_size=500", headers=auth_headers)
        assert response.status_code == 400
