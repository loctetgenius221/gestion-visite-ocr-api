"""Gestion administrative des visites : correction, annulation, export, audit, analytics."""

from __future__ import annotations

from datetime import datetime, timedelta

from httpx import AsyncClient


async def _creer_visite(client: AsyncClient, auth_headers: dict[str, str], seeded: dict) -> dict:
    response = await client.post(
        "/visits",
        headers=auth_headers,
        json={
            "visitor": {
                "prenom": "Awa",
                "nom": "Diop",
                "type_document": "CNI",
                "numero_document": "1234567890123456",
            },
            "service_id": str(seeded["service"].id),  # type: ignore[union-attr]
            "agent_id": str(seeded["agent"].id),  # type: ignore[union-attr]
            "purpose_id": str(seeded["purpose"].id),  # type: ignore[union-attr]
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestCorrection:
    async def test_le_motif_est_obligatoire(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.patch(
            f"/visits/{visite['id']}", headers=admin_headers, json={"badge_number": "B12"}
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_correction_du_badge_et_trace_dans_laudit(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.patch(
            f"/visits/{visite['id']}",
            headers=admin_headers,
            json={"reason": "Badge mal saisi à l'accueil", "badge_number": "B-042"},
        )

        assert response.status_code == 200
        assert response.json()["badge_number"] == "B-042"

        journal = await client.get(
            f"/audit-logs?entity=visit&entity_id={visite['id']}&action=visit.updated",
            headers=admin_headers,
        )
        entree = journal.json()["items"][0]
        assert entree["metadata"]["reason"] == "Badge mal saisi à l'accueil"
        # Le diff ne conserve que ce qui a changé.
        assert entree["metadata"]["changements"] == {
            "badge_number": {"avant": None, "après": "B-042"}
        }

    async def test_un_agent_dun_autre_service_est_refuse(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.patch(
            f"/visits/{visite['id']}",
            headers=admin_headers,
            json={"reason": "Mauvaise personne", "agent_id": str(seeded["other_agent"].id)},  # type: ignore[union-attr]
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "AGENT_SERVICE_MISMATCH"

    async def test_renseigner_la_sortie_cloture_la_visite(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        """Sinon la visite resterait « présente » avec une date de sortie."""
        visite = await _creer_visite(client, auth_headers, seeded)
        # Dérivée de l'entrée réelle : un horodatage en dur serait antérieur à
        # l'entrée dès que la suite tourne après l'heure choisie, et le service
        # le refuserait — à juste titre.
        sortie = datetime.fromisoformat(visite["checked_in_at"]) + timedelta(hours=1)

        response = await client.patch(
            f"/visits/{visite['id']}",
            headers=admin_headers,
            json={
                "reason": "Sortie oubliée par l'agent",
                "checked_out_at": sortie.isoformat(),
            },
        )

        assert response.status_code == 200
        assert response.json()["statut"] == "SORTI"

    async def test_une_sortie_anterieure_a_lentree_est_refusee(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.patch(
            f"/visits/{visite['id']}",
            headers=admin_headers,
            json={"reason": "Test", "checked_out_at": "2020-01-01T00:00:00Z"},
        )

        assert response.status_code == 422
        assert response.json()["error_code"] == "INVALID_VISIT_INTERVAL"


class TestRetraitDeLaPersonneRencontree:
    """Depuis l'ADR-019, `null` veut dire « efface » et non « inchangé »."""

    async def test_agent_id_nul_retire_la_personne(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, admin_headers: dict
    ):
        visite = await _creer_visite(client, auth_headers, seeded)
        assert visite["agent"] is not None

        response = await client.patch(
            f"/visits/{visite['id']}",
            json={"reason": "Dépôt de dossier, personne rencontrée saisie par erreur",
                  "agent_id": None},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent"] is None

    async def test_agent_id_omis_laisse_la_personne_en_place(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, admin_headers: dict
    ):
        """La nuance qui compte : omettre n'est pas effacer."""
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.patch(
            f"/visits/{visite['id']}",
            json={"reason": "Correction du badge seulement", "badge_number": "B-999"},
            headers=admin_headers,
        )

        assert response.status_code == 200
        assert response.json()["agent"]["name"] == "Aminata Diallo"

    async def test_changer_de_service_sans_agent_ne_declenche_aucun_conflit(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, admin_headers: dict
    ):
        """Sans personne rencontrée, il n'y a plus de cohérence agent/service à tenir."""
        visite = await _creer_visite(client, auth_headers, seeded)
        await client.patch(
            f"/visits/{visite['id']}",
            json={"reason": "Retrait de la personne rencontrée", "agent_id": None},
            headers=admin_headers,
        )

        response = await client.patch(
            f"/visits/{visite['id']}",
            json={
                "reason": "Le dépôt concernait en fait la DSI",
                "service_id": str(seeded["other_service"].id),
            },
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["service"]["code"] == "DSI"
        assert response.json()["agent"] is None

    async def test_le_service_ne_peut_pas_etre_efface(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, admin_headers: dict
    ):
        """Sans ce refus, le `null` partirait échouer en base sur un NOT NULL."""
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.patch(
            f"/visits/{visite['id']}",
            json={"reason": "Tentative d'effacement du service", "service_id": None},
            headers=admin_headers,
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"


class TestAnnulation:
    async def test_annulation_logique_conserve_la_visite(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.post(
            f"/visits/{visite['id']}/cancel",
            headers=admin_headers,
            json={"reason": "Doublon de saisie"},
        )

        assert response.status_code == 200
        assert response.json()["statut"] == "ANNULEE"
        assert response.json()["cancellation_reason"] == "Doublon de saisie"
        assert response.json()["cancelled_at"] is not None

        # Elle reste lisible : rien n'est supprimé.
        detail = await client.get(f"/visits/{visite['id']}", headers=admin_headers)
        assert detail.status_code == 200

    async def test_une_visite_annulee_ne_se_cloture_plus(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)
        await client.post(
            f"/visits/{visite['id']}/cancel", headers=admin_headers, json={"reason": "Erreur"}
        )

        response = await client.put(f"/visits/{visite['id']}/checkout", headers=auth_headers)

        assert response.status_code == 409
        assert response.json()["error_code"] == "VISIT_CANCELLED"

    async def test_annuler_deux_fois_est_un_conflit(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)
        await client.post(
            f"/visits/{visite['id']}/cancel", headers=admin_headers, json={"reason": "Erreur"}
        )

        response = await client.post(
            f"/visits/{visite['id']}/cancel", headers=admin_headers, json={"reason": "Encore"}
        )

        assert response.status_code == 409

    async def test_une_visite_annulee_sort_des_statistiques(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)
        avant = (await client.get("/dashboard/stats", headers=auth_headers)).json()

        await client.post(
            f"/visits/{visite['id']}/cancel", headers=admin_headers, json={"reason": "Doublon"}
        )

        apres = (await client.get("/dashboard/stats", headers=auth_headers)).json()
        assert apres["visites_du_jour"] == avant["visites_du_jour"] - 1
        assert apres["presents_actuellement"] == avant["presents_actuellement"] - 1


class TestSuppression:
    async def test_la_visite_disparait_du_registre(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.delete(f"/visits/{visite['id']}", headers=admin_headers)

        assert response.status_code == 204
        assert response.content == b""
        detail = await client.get(f"/visits/{visite['id']}", headers=admin_headers)
        assert detail.status_code == 404
        assert detail.json()["error_code"] == "VISIT_NOT_FOUND"

    async def test_supprimer_deux_fois_repond_404(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)
        await client.delete(f"/visits/{visite['id']}", headers=admin_headers)

        response = await client.delete(f"/visits/{visite['id']}", headers=admin_headers)

        assert response.status_code == 404
        assert response.json()["error_code"] == "VISIT_NOT_FOUND"

    async def test_un_agent_de_controle_ne_peut_pas_supprimer(
        self, client: AsyncClient, auth_headers: dict[str, str], seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)

        response = await client.delete(f"/visits/{visite['id']}", headers=auth_headers)

        assert response.status_code == 403
        assert response.json()["error_code"] == "FORBIDDEN"
        # La visite est toujours là : le refus n'a rien détruit au passage.
        detail = await client.get(f"/visits/{visite['id']}", headers=auth_headers)
        assert detail.status_code == 200

    async def test_le_journal_conserve_un_instantane_de_ce_qui_a_ete_detruit(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        """C'est la seule trace restante : elle doit répondre « que contenait-elle ? »."""
        visite = await _creer_visite(client, auth_headers, seeded)

        await client.delete(f"/visits/{visite['id']}", headers=admin_headers)

        journal = await client.get(
            f"/audit-logs?entity=visit&entity_id={visite['id']}&action=visit.deleted",
            headers=admin_headers,
        )
        assert journal.status_code == 200
        entrees = journal.json()["items"]
        assert len(entrees) == 1
        assert entrees[0]["actor_identifiant"] == "admin001"

        instantane = entrees[0]["metadata"]["visite"]
        assert instantane["visiteur"] == "Awa Diop"
        assert instantane["numero_document"] == "1234567890123456"
        assert instantane["service_id"] == str(seeded["service"].id)  # type: ignore[union-attr]
        assert instantane["statut"] == "PRESENT"
        assert instantane["checked_in_at"] is not None

    async def test_le_visiteur_survit_a_la_suppression_de_sa_visite(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        """Sa fiche sert aux autres passages : seule la visite est détruite."""
        visite = await _creer_visite(client, auth_headers, seeded)

        await client.delete(f"/visits/{visite['id']}", headers=admin_headers)

        recherche = await client.get("/visitors?search=Diop", headers=auth_headers)
        assert recherche.status_code == 200
        assert [v["nom"] for v in recherche.json()["items"]] == ["Diop"]

    async def test_la_visite_supprimee_sort_des_statistiques(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        avant = (await client.get("/dashboard/stats", headers=auth_headers)).json()
        visite = await _creer_visite(client, auth_headers, seeded)

        await client.delete(f"/visits/{visite['id']}", headers=admin_headers)

        apres = (await client.get("/dashboard/stats", headers=auth_headers)).json()
        assert apres == avant

    async def test_le_visiteur_peut_a_nouveau_entrer(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        """La visite ouverte ayant disparu, plus rien ne bloque un nouvel enregistrement."""
        visite = await _creer_visite(client, auth_headers, seeded)
        await client.delete(f"/visits/{visite['id']}", headers=admin_headers)

        response = await client.post(
            "/visits",
            headers=auth_headers,
            json={
                "visitor": {
                    "prenom": "Awa",
                    "nom": "Diop",
                    "type_document": "CNI",
                    "numero_document": "1234567890123456",
                },
                "service_id": str(seeded["service"].id),  # type: ignore[union-attr]
                "purpose_id": str(seeded["purpose"].id),  # type: ignore[union-attr]
            },
        )

        assert response.status_code == 201


class TestFiltresEtendus:
    async def test_filtre_par_service(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        await _creer_visite(client, auth_headers, seeded)

        bon = await client.get(
            f"/visits?service_id={seeded['service'].id}", headers=admin_headers  # type: ignore[union-attr]
        )
        autre = await client.get(
            f"/visits?service_id={seeded['other_service'].id}", headers=admin_headers  # type: ignore[union-attr]
        )

        assert bon.json()["total"] == 1
        assert autre.json()["total"] == 0

    async def test_filtre_par_auteur(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        await _creer_visite(client, auth_headers, seeded)

        response = await client.get(
            f"/visits?created_by={seeded['user'].id}", headers=admin_headers  # type: ignore[union-attr]
        )

        assert response.json()["total"] == 1


class TestExport:
    async def test_export_csv(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        await _creer_visite(client, auth_headers, seeded)

        response = await client.get("/visits/export?format=csv", headers=admin_headers)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=" in response.headers["content-disposition"]

        texte = response.content.decode("utf-8")
        # BOM UTF-8 en tête : sans lui, Excel sous Windows abîme les accents.
        assert texte.startswith("﻿")
        assert "Nom du visiteur" in texte
        assert "Diop" in texte
        # Séparateur point-virgule, attendu par Excel en configuration francophone.
        assert ";" in texte.splitlines()[0]

    async def test_le_pdf_repond_501_sans_pretendre_lavoir_produit(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/visits/export?format=pdf", headers=admin_headers)

        assert response.status_code == 501
        assert response.json()["error_code"] == "EXPORT_FORMAT_UNAVAILABLE"

    async def test_les_filtres_sappliquent_a_lexport(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        await _creer_visite(client, auth_headers, seeded)

        response = await client.get(
            f"/visits/export?format=csv&service_id={seeded['other_service'].id}",  # type: ignore[union-attr]
            headers=admin_headers,
        )

        # En-tête seul : aucune visite ne correspond.
        assert len(response.content.decode("utf-8").strip().splitlines()) == 1


class TestAuditLog:
    async def test_les_connexions_reussies_et_echouees_sont_tracees(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "faux"}
        )

        journal = await client.get("/audit-logs?action=auth", headers=admin_headers)
        actions = {entree["action"] for entree in journal.json()["items"]}

        assert "auth.login.failed" in actions
        assert "auth.login.success" in actions  # celle de l'admin lui-même

    async def test_le_filtre_par_action_fonctionne_par_prefixe(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        visite = await _creer_visite(client, auth_headers, seeded)
        await client.put(f"/visits/{visite['id']}/checkout", headers=auth_headers)

        response = await client.get("/audit-logs?action=visit", headers=admin_headers)
        actions = {entree["action"] for entree in response.json()["items"]}

        assert {"visit.created", "visit.checkout"} <= actions

    async def test_la_creation_de_compte_est_tracee_avec_son_auteur(
        self, client: AsyncClient, admin_headers: dict[str, str], admin
    ) -> None:
        await client.post(
            "/users",
            headers=admin_headers,
            json={"nom": "Traçable", "identifiant": "agent099", "role": "AGENT_CONTROLE"},
        )

        journal = await client.get("/audit-logs?action=user.created", headers=admin_headers)
        entree = journal.json()["items"][0]

        assert entree["actor_id"] == str(admin.id)
        assert entree["actor_identifiant"] == "admin001"
        assert entree["metadata"]["identifiant"] == "agent099"

    async def test_le_journal_est_en_lecture_seule(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Aucune route d'écriture ni de suppression n'est exposée."""
        for methode in ("POST", "DELETE", "PUT", "PATCH"):
            response = await client.request(methode, "/audit-logs", headers=admin_headers)
            assert response.status_code == 405


class TestParametresSysteme:
    async def test_lecture_des_valeurs_par_defaut(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/settings", headers=admin_headers)

        assert response.status_code == 200
        assert response.json()["max_failed_login_attempts"] == 5
        assert response.json()["visit_long_duration_alert_minutes"] == 120

    async def test_mise_a_jour_partielle_et_effet_immediat(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        maj = await client.put(
            "/settings", headers=admin_headers, json={"max_failed_login_attempts": 3}
        )

        assert maj.status_code == 200
        assert maj.json()["max_failed_login_attempts"] == 3
        # Les autres paramètres gardent leur défaut.
        assert maj.json()["visit_long_duration_alert_minutes"] == 120

        # Le nouveau seuil s'applique sans redéploiement.
        for _ in range(3):
            await client.post(
                "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "faux"}
            )
        response = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "MotDePasse123!"}
        )
        assert response.json()["error_code"] == "LOCKED_ACCOUNT"

    async def test_valeur_hors_bornes_refusee(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.put(
            "/settings", headers=admin_headers, json={"max_failed_login_attempts": 1}
        )

        assert response.status_code == 400


class TestAnalytics:
    async def test_serie_temporelle_sans_trou(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        await _creer_visite(client, auth_headers, seeded)

        response = await client.get(
            "/dashboard/stats/timeseries?granularity=day"
            "&date_from=2026-08-01T00:00:00Z&date_to=2026-08-10T00:00:00Z",
            headers=admin_headers,
        )

        assert response.status_code == 200
        # Du 1er au 10 inclus : dix tranches, y compris les jours sans visite.
        assert len(response.json()["points"]) == 10
        assert response.json()["granularity"] == "day"

    async def test_repartition_par_service(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        await _creer_visite(client, auth_headers, seeded)

        response = await client.get("/dashboard/stats/by-service", headers=admin_headers)

        assert response.json()["total"] == 1
        assert response.json()["items"][0]["label"] == "Direction des RH"
        assert response.json()["items"][0]["pourcentage"] == 100.0

    async def test_les_heures_de_pointe_couvrent_les_24_heures(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/dashboard/stats/peak-hours", headers=admin_headers)

        assert [item["heure"] for item in response.json()["heures"]] == list(range(24))

    async def test_duree_moyenne_sur_periode_vide(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Aucune visite clôturée : pas de division par zéro, des champs nuls."""
        response = await client.get("/dashboard/stats/avg-duration", headers=admin_headers)

        assert response.status_code == 200
        assert response.json()["visites_cloturees"] == 0
        assert response.json()["duree_moyenne_minutes"] is None

    async def test_top_agents(
        self, client: AsyncClient, admin_headers: dict[str, str], auth_headers, seeded: dict
    ) -> None:
        await _creer_visite(client, auth_headers, seeded)

        response = await client.get("/dashboard/stats/top-agents?limit=5", headers=admin_headers)

        assert response.status_code == 200
        assert response.json()["items"][0]["agent_name"] == "Aminata Diallo"
        assert response.json()["items"][0]["visites"] == 1

    async def test_des_bornes_inversees_sont_remises_a_lendroit(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get(
            "/dashboard/stats/by-service"
            "?date_from=2026-08-10T00:00:00Z&date_to=2026-08-01T00:00:00Z",
            headers=admin_headers,
        )

        assert response.status_code == 200
        periode = response.json()["period"]
        assert periode["date_from"] < periode["date_to"]
