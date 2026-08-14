"""Recherche des visiteurs déjà connus (ADR-017)."""

from __future__ import annotations

from httpx import AsyncClient

from tests.integration.test_visits import visit_payload


async def _enregistrer(
    client: AsyncClient, seeded: dict, auth_headers: dict, **visiteur
) -> dict:
    """Crée une visite pour un visiteur dont on surcharge quelques champs."""
    payload = visit_payload(seeded)
    payload["visitor"].update(visiteur)
    response = await client.post("/visits", json=payload, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


class TestRechercheVisiteurs:
    async def test_recherche_par_nom(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        await _enregistrer(client, seeded, auth_headers)

        response = await client.get(
            "/visitors", params={"search": "diop"}, headers=auth_headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["nom"] == "Diop"

    async def test_recherche_par_prenom(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        await _enregistrer(client, seeded, auth_headers)

        response = await client.get(
            "/visitors", params={"search": "amin"}, headers=auth_headers
        )

        assert response.json()["total"] == 1

    async def test_recherche_par_numero_de_document(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        await _enregistrer(client, seeded, auth_headers)

        response = await client.get(
            "/visitors", params={"search": "1234567890123"}, headers=auth_headers
        )

        assert response.json()["total"] == 1

    async def test_recherche_par_nin_saisi_par_blocs(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Le NIN est imprimé par blocs et l'agent le recopie tel quel (ADR-016)."""
        await _enregistrer(client, seeded, auth_headers, nin="2K05201200108")

        response = await client.get(
            "/visitors", params={"search": "2 K05 2012 00108"}, headers=auth_headers
        )

        assert response.json()["total"] == 1

    async def test_aucun_resultat_renvoie_une_page_vide(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        await _enregistrer(client, seeded, auth_headers)

        response = await client.get(
            "/visitors", params={"search": "ndiaye"}, headers=auth_headers
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}

    async def test_moins_de_trois_caracteres_est_refuse(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Une lettre suffirait à parcourir tout le fichier des visiteurs."""
        response = await client.get(
            "/visitors", params={"search": "di"}, headers=auth_headers
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_terme_de_ponctuation_ne_remonte_pas_tout_le_fichier(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Compacté, « --- » deviendrait `%%` sur les colonnes de numéros."""
        await _enregistrer(client, seeded, auth_headers)

        response = await client.get(
            "/visitors", params={"search": "---"}, headers=auth_headers
        )

        assert response.json()["total"] == 0

    async def test_sans_authentification_renvoie_401(self, client: AsyncClient, seeded: dict):
        response = await client.get("/visitors", params={"search": "diop"})
        assert response.status_code == 401


class TestEtatDuVisiteurDansLaRecherche:
    async def test_visite_ouverte_est_signalee(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        visite = await _enregistrer(client, seeded, auth_headers)

        response = await client.get(
            "/visitors", params={"search": "diop"}, headers=auth_headers
        )

        fiche = response.json()["items"][0]
        assert fiche["visite_ouverte_id"] == visite["id"]
        assert fiche["derniere_visite_at"] is not None

    async def test_apres_cloture_plus_aucune_visite_ouverte(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        visite = await _enregistrer(client, seeded, auth_headers)
        await client.put(f"/visits/{visite['id']}/checkout", headers=auth_headers)

        response = await client.get(
            "/visitors", params={"search": "diop"}, headers=auth_headers
        )

        fiche = response.json()["items"][0]
        assert fiche["visite_ouverte_id"] is None
        # La venue reste datée : c'est elle qui trie la liste.
        assert fiche["derniere_visite_at"] is not None

    async def test_une_visite_annulee_ne_compte_pas_comme_derniere_venue(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, admin_headers: dict
    ):
        visite = await _enregistrer(client, seeded, auth_headers)
        await client.post(
            f"/visits/{visite['id']}/cancel",
            json={"reason": "Saisie erronée"},
            headers=admin_headers,
        )

        response = await client.get(
            "/visitors", params={"search": "diop"}, headers=auth_headers
        )

        fiche = response.json()["items"][0]
        assert fiche["derniere_visite_at"] is None
        assert fiche["visite_ouverte_id"] is None

    async def test_le_visiteur_le_plus_recent_arrive_en_tete(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        ancien = await _enregistrer(
            client, seeded, auth_headers, nom="Diop", numero_document="1111111111111"
        )
        await client.put(f"/visits/{ancien['id']}/checkout", headers=auth_headers)
        await _enregistrer(
            client,
            seeded,
            auth_headers,
            nom="Diop",
            prenom="Moussa",
            numero_document="2222222222222",
        )

        response = await client.get(
            "/visitors", params={"search": "diop"}, headers=auth_headers
        )

        body = response.json()
        assert body["total"] == 2
        assert body["items"][0]["prenom"] == "Moussa"


class TestEnchainementRechercheEtEnregistrement:
    async def test_le_parcours_complet_sans_rescan(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Le geste visé : retrouver la fiche, puis enregistrer sans la pièce."""
        premiere = await _enregistrer(client, seeded, auth_headers)
        await client.put(f"/visits/{premiere['id']}/checkout", headers=auth_headers)

        trouve = await client.get(
            "/visitors", params={"search": "diop"}, headers=auth_headers
        )
        visitor_id = trouve.json()["items"][0]["id"]

        payload = visit_payload(seeded)
        del payload["visitor"]
        payload["visitor_id"] = visitor_id
        seconde = await client.post("/visits", json=payload, headers=auth_headers)

        assert seconde.status_code == 201, seconde.text
        assert seconde.json()["visitor"]["id"] == visitor_id
