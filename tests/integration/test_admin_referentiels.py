"""Référentiels en écriture : création, modification, archivage logique."""

from __future__ import annotations

from httpx import AsyncClient


class TestServices:
    async def test_creation_et_lecture(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers: dict[str, str]
    ) -> None:
        creation = await client.post(
            "/services",
            headers=admin_headers,
            json={"name": "Direction du Budget", "code": "DB", "floor": "5e étage"},
        )

        assert creation.status_code == 201
        assert creation.json()["status"] == "active"

        # L'agent de contrôle voit immédiatement le nouveau service.
        codes = {
            service["code"]
            for service in (await client.get("/services", headers=auth_headers)).json()
        }
        assert "DB" in codes

    async def test_code_en_doublon_refuse(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/services", headers=admin_headers, json={"name": "Autre", "code": "drh"}
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "DUPLICATE_REFERENTIEL"

    async def test_un_service_ne_peut_pas_devenir_son_propre_ancetre(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        """Un cycle ferait boucler indéfiniment la reconstruction de l'arbre."""
        parent_id = str(seeded["service"].id)  # type: ignore[union-attr]
        enfant = await client.post(
            "/services",
            headers=admin_headers,
            json={"name": "Sous-direction", "code": "SDRH", "parent_id": parent_id},
        )
        enfant_id = enfant.json()["id"]

        response = await client.put(
            f"/services/{parent_id}", headers=admin_headers, json={"parent_id": enfant_id}
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "SERVICE_HIERARCHY_CYCLE"

    async def test_archiver_refuse_tant_quil_reste_des_agents_actifs(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        service_id = str(seeded["service"].id)  # type: ignore[union-attr]

        response = await client.patch(
            f"/services/{service_id}/status", headers=admin_headers, json={"status": "archived"}
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "SERVICE_HAS_ACTIVE_AGENTS"

    async def test_archiver_retire_de_la_liste_mobile_sans_supprimer(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers: dict[str, str]
    ) -> None:
        service_id = (
            await client.post(
                "/services", headers=admin_headers, json={"name": "Éphémère", "code": "EPH"}
            )
        ).json()["id"]

        archivage = await client.patch(
            f"/services/{service_id}/status", headers=admin_headers, json={"status": "archived"}
        )
        assert archivage.status_code == 200
        assert archivage.json()["status"] == "archived"
        assert archivage.json()["archived_at"] is not None

        visibles = {s["id"] for s in (await client.get("/services", headers=auth_headers)).json()}
        assert service_id not in visibles

        # Rien n'est supprimé : le dashboard le retrouve en le demandant.
        archives = {
            s["id"]
            for s in (
                await client.get("/services?include_archived=true", headers=admin_headers)
            ).json()
        }
        assert service_id in archives

    async def test_desarchiver_le_remet_en_service(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers: dict[str, str]
    ) -> None:
        service_id = (
            await client.post(
                "/services", headers=admin_headers, json={"name": "Retour", "code": "RET"}
            )
        ).json()["id"]
        await client.patch(
            f"/services/{service_id}/status", headers=admin_headers, json={"status": "archived"}
        )

        response = await client.patch(
            f"/services/{service_id}/status", headers=admin_headers, json={"status": "active"}
        )

        assert response.json()["status"] == "active"
        assert response.json()["archived_at"] is None
        visibles = {s["id"] for s in (await client.get("/services", headers=auth_headers)).json()}
        assert service_id in visibles


class TestAgentsEtMotifs:
    async def test_agent_rattache_a_un_service_archive_refuse(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        service_id = (
            await client.post(
                "/services", headers=admin_headers, json={"name": "Fermé", "code": "FRM"}
            )
        ).json()["id"]
        await client.patch(
            f"/services/{service_id}/status", headers=admin_headers, json={"status": "archived"}
        )

        response = await client.post(
            "/agents",
            headers=admin_headers,
            json={"name": "Nouveau", "service_id": service_id},
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "ARCHIVED_REFERENTIEL"

    async def test_motif_en_doublon_refuse(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/purposes", headers=admin_headers, json={"libelle": "rendez-vous professionnel"}
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "DUPLICATE_REFERENTIEL"

    async def test_creation_et_modification_dun_motif(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        creation = await client.post(
            "/purposes", headers=admin_headers, json={"libelle": "Audit interne"}
        )
        assert creation.status_code == 201

        modification = await client.put(
            f"/purposes/{creation.json()['id']}",
            headers=admin_headers,
            json={"libelle": "Audit interne et contrôle"},
        )
        assert modification.status_code == 200
        assert modification.json()["libelle"] == "Audit interne et contrôle"


class TestUsageDesReferentielsArchives:
    async def test_creer_une_visite_sur_un_motif_archive_est_refuse(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        auth_headers: dict[str, str],
        seeded: dict,
    ) -> None:
        """Un client au cache périmé ne doit pas pouvoir ressusciter une entrée archivée."""
        purpose_id = str(seeded["purpose"].id)  # type: ignore[union-attr]
        await client.patch(
            f"/purposes/{purpose_id}/status", headers=admin_headers, json={"status": "archived"}
        )

        response = await client.post(
            "/visits",
            headers=auth_headers,
            json={
                "visitor": {
                    "prenom": "Awa",
                    "nom": "Diop",
                    "type_document": "CNI",
                    "numero_document": "1234567890",
                },
                "service_id": str(seeded["service"].id),  # type: ignore[union-attr]
                "agent_id": str(seeded["agent"].id),  # type: ignore[union-attr]
                "purpose_id": purpose_id,
            },
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "ARCHIVED_REFERENTIEL"
