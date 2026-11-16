"""Travail hors-ligne de l'app mobile (ADR-025).

Couvre la synchronisation des entrées et des sorties, son idempotence, l'ordre
chronologique, les garde-fous d'horloge et de cloisonnement, ainsi que les
lectures qui préparent le hors-ligne (`/sync/referentiels`, `/sync/presents`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit_log import AuditLog
from app.models.base import ensure_utc
from app.models.enums import CheckoutOrigin, ForgottenDocumentStatus, VisitStatus
from app.models.forgotten_document import ForgottenDocumentCase
from app.models.organization import Organization
from app.models.referentiel import Purpose
from app.models.visit import Visit
from tests.integration.test_visits import visit_payload


def _il_y_a(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _meme_instant(lu: str, envoye: str) -> bool:
    """SQLite, la base de test, relit les dates sans fuseau (ADR-007) : UTC."""
    return ensure_utc(datetime.fromisoformat(lu)) == datetime.fromisoformat(envoye)


def entree(seeded: dict, reference: str, numero: str, *, a: str, **overrides) -> dict:
    payload = visit_payload(seeded, client_reference=reference, checked_in_at=a, **overrides)
    payload["visitor"]["numero_document"] = numero
    return payload


async def _sync(client: AsyncClient, headers: dict, **lot) -> dict:
    reponse = await client.post("/visits/sync", json=lot, headers=headers)
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


class TestSortiesHorsLigne:
    async def test_entree_et_sortie_du_meme_lot(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        sortie_a = _il_y_a(hours=1)
        resultat = await _sync(
            client,
            auth_headers,
            visits=[entree(seeded, "REF-ALLER-01", "AR01", a=_il_y_a(hours=3))],
            checkouts=[{"client_reference": "REF-ALLER-01", "checked_out_at": sortie_a}],
        )

        assert resultat["created"] == 1
        sortie = resultat["checkout_results"][0]
        assert sortie["status"] == "closed"
        visite = await client.get(f"/visits/{sortie['visit_id']}", headers=auth_headers)
        corps = visite.json()
        assert corps["statut"] == "SORTI"
        assert corps["checkout_origin"] == "MANUAL"
        assert corps["recorded_offline"] is True
        # L'heure de la tablette, pas celle de la synchro.
        assert _meme_instant(corps["checked_out_at"], sortie_a)

    async def test_aller_retour_rejoue_dans_l_ordre_chronologique(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Même personne, deux venues pendant la coupure. Le lot arrive dans le
        désordre : rejoué tel quel, la seconde entrée serait refusée."""
        resultat = await _sync(
            client,
            auth_headers,
            visits=[
                entree(seeded, "REF-VENUE-02", "AR02", a=_il_y_a(hours=1)),
                entree(seeded, "REF-VENUE-01", "AR02", a=_il_y_a(hours=4)),
            ],
            checkouts=[{"client_reference": "REF-VENUE-01", "checked_out_at": _il_y_a(hours=3)}],
        )

        assert [r["status"] for r in resultat["results"]] == ["created", "created"]
        assert resultat["checkout_results"][0]["status"] == "closed"

    async def test_sortie_d_une_visite_enregistree_en_ligne(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        creee = await client.post(
            "/visits",
            json=visit_payload(seeded, checked_in_at=_il_y_a(hours=2)),
            headers=auth_headers,
        )

        resultat = await _sync(
            client,
            auth_headers,
            checkouts=[{"visit_id": creee.json()["id"], "checked_out_at": _il_y_a(minutes=30)}],
        )

        assert resultat["checkout_closed"] == 1
        assert resultat["total"] == 0
        assert creee.json()["recorded_offline"] is False

    async def test_sortie_renvoyee_est_un_succes_idempotent(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        lot = {
            "visits": [entree(seeded, "REF-REJEU-01", "RJ01", a=_il_y_a(hours=2))],
            "checkouts": [
                {"client_reference": "REF-REJEU-01", "checked_out_at": _il_y_a(hours=1)}
            ],
        }
        premier = await _sync(client, auth_headers, **lot)

        rejeu = await _sync(client, auth_headers, **lot)

        assert rejeu["results"][0]["status"] == "already_synced"
        sortie = rejeu["checkout_results"][0]
        assert sortie["status"] == "already_closed"
        assert sortie["checked_out_at"] == premier["checkout_results"][0]["checked_out_at"]

    async def test_sortie_anterieure_a_l_entree(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        resultat = await _sync(
            client,
            auth_headers,
            visits=[entree(seeded, "REF-INTERV-1", "IV01", a=_il_y_a(hours=1))],
            checkouts=[{"client_reference": "REF-INTERV-1", "checked_out_at": _il_y_a(hours=2)}],
        )

        sortie = resultat["checkout_results"][0]
        # La sortie, plus ancienne, est rejouée avant l'entrée : la visite n'existe
        # pas encore à ce moment-là.
        assert sortie["status"] == "error"
        assert sortie["error_code"] == "VISIT_NOT_FOUND"

    async def test_sortie_anterieure_a_une_entree_deja_connue(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        visite = entree(seeded, "REF-INTERV-2", "IV02", a=_il_y_a(hours=1))
        await _sync(client, auth_headers, visits=[visite])

        resultat = await _sync(
            client,
            auth_headers,
            checkouts=[{"client_reference": "REF-INTERV-2", "checked_out_at": _il_y_a(hours=2)}],
        )

        assert resultat["checkout_results"][0]["error_code"] == "INVALID_VISIT_INTERVAL"

    async def test_sortie_d_une_visite_annulee(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, admin_headers: dict
    ):
        creee = await client.post(
            "/visits",
            json=visit_payload(seeded, checked_in_at=_il_y_a(hours=2)),
            headers=auth_headers,
        )
        visit_id = creee.json()["id"]
        await client.post(
            f"/visits/{visit_id}/cancel", json={"reason": "Doublon"}, headers=admin_headers
        )

        resultat = await _sync(
            client,
            auth_headers,
            checkouts=[{"visit_id": visit_id, "checked_out_at": _il_y_a(hours=1)}],
        )

        sortie = resultat["checkout_results"][0]
        assert (sortie["status"], sortie["error_code"]) == ("conflict", "VISIT_CANCELLED")

    async def test_sortie_corrige_une_cloture_automatique(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        """La tâche de 17 h a clôturé la visite faute de sortie connue ; la
        tablette revient avec la vraie heure. Elle l'emporte, et la fiche de pièce
        oubliée est résolue."""
        creee = await client.post(
            "/visits",
            json=visit_payload(seeded, checked_in_at=_il_y_a(hours=5)),
            headers=auth_headers,
        )
        visit_id = uuid.UUID(creee.json()["id"])
        visite = await session.get(Visit, visit_id)
        visite.statut = VisitStatus.SORTI
        visite.checkout_origin = CheckoutOrigin.AUTOMATIC
        visite.checked_out_at = datetime.now(UTC) - timedelta(minutes=10)
        fiche = ForgottenDocumentCase(
            organization_id=visite.organization_id,
            visit_id=visite.id,
            visitor_id=visite.visitor_id,
            status=ForgottenDocumentStatus.PENDING,
        )
        session.add(fiche)
        await session.commit()

        vraie_sortie = _il_y_a(hours=3)
        resultat = await _sync(
            client,
            auth_headers,
            checkouts=[{"visit_id": str(visit_id), "checked_out_at": vraie_sortie}],
        )

        sortie = resultat["checkout_results"][0]
        assert sortie["status"] == "closed"
        assert sortie["details"] == {"cloture_automatique_corrigee": True}
        corps = (await client.get(f"/visits/{visit_id}", headers=auth_headers)).json()
        assert corps["checkout_origin"] == "MANUAL"
        assert _meme_instant(corps["checked_out_at"], vraie_sortie)
        await session.refresh(fiche)
        assert fiche.status is ForgottenDocumentStatus.RESOLVED

    async def test_lot_de_sorties_seules_et_lot_vide(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        vide = await client.post(
            "/visits/sync", json={"visits": [], "checkouts": []}, headers=auth_headers
        )
        assert vide.status_code == 400

        designation_double = await client.post(
            "/visits/sync",
            json={
                "checkouts": [
                    {
                        "visit_id": str(uuid.uuid4()),
                        "client_reference": "REF-DOUBLE-1",
                        "checked_out_at": _il_y_a(hours=1),
                    }
                ]
            },
            headers=auth_headers,
        )
        assert designation_double.status_code == 400


class TestGardeFousDesEntrees:
    async def test_heure_sans_fuseau_refusee(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        naive = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        reponse = await client.post(
            "/visits/sync",
            json={"visits": [entree(seeded, "REF-NAIVE-01", "NV01", a=naive)]},
            headers=auth_headers,
        )

        assert reponse.status_code == 400

    async def test_reference_et_heure_obligatoires(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        reponse = await client.post(
            "/visits/sync", json={"visits": [visit_payload(seeded)]}, headers=auth_headers
        )

        assert reponse.status_code == 400

    async def test_heure_dans_le_futur(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        futur = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

        resultat = await _sync(
            client, auth_headers, visits=[entree(seeded, "REF-FUTUR-01", "FT01", a=futur)]
        )
        en_ligne = await client.post(
            "/visits", json=visit_payload(seeded, checked_in_at=futur), headers=auth_headers
        )

        assert resultat["results"][0]["error_code"] == "TIMESTAMP_IN_FUTURE"
        assert en_ligne.status_code == 422
        assert en_ligne.json()["error_code"] == "TIMESTAMP_IN_FUTURE"

    async def test_une_petite_avance_d_horloge_est_toleree(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        avance = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()

        resultat = await _sync(
            client, auth_headers, visits=[entree(seeded, "REF-AVANCE-1", "AV01", a=avance)]
        )

        assert resultat["created"] == 1

    async def test_conflit_de_presence_porte_la_visite_ouverte(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        ouverte = await client.post(
            "/visits",
            json=entree(seeded, "REF-OUVERT-1", "PR01", a=_il_y_a(hours=3)),
            headers=auth_headers,
        )

        seconde = entree(seeded, "REF-OUVERT-2", "PR01", a=_il_y_a(hours=1))
        resultat = await _sync(client, auth_headers, visits=[seconde])

        item = resultat["results"][0]
        assert (item["status"], item["error_code"]) == ("conflict", "VISITOR_ALREADY_PRESENT")
        assert item["details"]["visit_id"] == ouverte.json()["id"]

    async def test_entree_synchronisee_tracee_au_journal(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        visite = entree(seeded, "REF-AUDIT-01", "AU01", a=_il_y_a(hours=1))
        resultat = await _sync(client, auth_headers, visits=[visite])

        trace = (
            await session.execute(
                select(AuditLog).where(AuditLog.entity_id == resultat["results"][0]["visit_id"])
            )
        ).scalar_one()
        assert trace.action == "visit.created"
        assert trace.meta["source"] == "sync"


class TestCloisonnementDeLaSynchro:
    async def test_service_d_une_autre_organisation_sans_motif_propre(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        """Faille corrigée : sans `organization_id` ni motif propre pour la
        trahir, l'entrée prenait l'organisation de son service et écrivait dans
        le registre de la voisine."""
        payload = entree(
            seeded,
            "REF-INTRUS-01",
            "IN01",
            a=_il_y_a(hours=1),
            service_id=str(voisine["service"].id),
            agent_id=None,
            purpose_id=None,
            motif_libre="Livraison",
        )

        resultat = await _sync(client, auth_headers, visits=[payload])

        assert resultat["results"][0]["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"

    async def test_sortie_d_une_visite_de_la_voisine(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        resultat = await _sync(
            client,
            auth_headers,
            checkouts=[
                {"visit_id": str(voisine["visit"].id), "checked_out_at": _il_y_a(minutes=5)}
            ],
        )

        assert resultat["checkout_results"][0]["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"


class TestReferentielsHorsLigne:
    async def test_instantane_de_l_organisation_avec_etag(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        reponse = await client.get("/sync/referentiels", headers=auth_headers)

        assert reponse.status_code == 200, reponse.text
        corps = reponse.json()
        assert reponse.headers["etag"] == f'"{corps["version"]}"'
        assert corps["organization"]["code"] == "MIN-INTERIEUR"
        assert {s["code"] for s in corps["services"]} == {"DRH", "DSI"}
        assert len(corps["agents"]) == 2
        # Rien de la voisine.
        assert str(voisine["service"].id) not in {s["id"] for s in corps["services"]}
        assert set(corps["parametres"]) == {
            "automatic_visit_closure_enabled",
            "automatic_visit_closure_time",
            "visit_long_duration_alert_minutes",
            "server_ocr_enabled",
        }

    async def test_304_tant_que_rien_ne_change(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        etag = (await client.get("/sync/referentiels", headers=auth_headers)).headers["etag"]

        inchange = await client.get(
            "/sync/referentiels", headers={**auth_headers, "If-None-Match": etag}
        )
        assert inchange.status_code == 304
        assert inchange.content == b""

        session.add(Purpose(libelle="Audit interne", organization_id=seeded["organization"].id))
        await session.commit()
        change = await client.get(
            "/sync/referentiels", headers={**auth_headers, "If-None-Match": etag}
        )
        assert change.status_code == 200
        assert change.headers["etag"] != etag

    async def test_l_archive_sort_de_l_instantane(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, admin_headers: dict
    ):
        archive = await client.patch(
            f"/purposes/{seeded['purpose'].id}/status",
            json={"status": "archived"},
            headers=admin_headers,
        )
        assert archive.status_code == 200, archive.text

        corps = (await client.get("/sync/referentiels", headers=auth_headers)).json()

        assert corps["purposes"] == []

    async def test_admin_de_plateforme_doit_nommer_l_organisation(
        self, client: AsyncClient, seeded: dict, admin_headers: dict
    ):
        sans = await client.get("/sync/referentiels", headers=admin_headers)
        avec = await client.get(
            f"/sync/referentiels?organization_id={seeded['organization'].id}",
            headers=admin_headers,
        )

        assert sans.status_code == 400
        assert sans.json()["error_code"] == "ORGANIZATION_REQUIRED"
        assert avec.status_code == 200


class TestPresentsHorsLigne:
    async def test_visites_ouvertes_de_l_organisation_seulement(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, voisine: dict
    ):
        ouverte = await client.post(
            "/visits",
            json=entree(seeded, "REF-PRES-01", "PS01", a=_il_y_a(hours=2)),
            headers=auth_headers,
        )
        fermee = await client.post(
            "/visits",
            json=entree(seeded, "REF-PRES-02", "PS02", a=_il_y_a(hours=1)),
            headers=auth_headers,
        )
        await client.put(f"/visits/{fermee.json()['id']}/checkout", headers=auth_headers)

        corps = (await client.get("/sync/presents", headers=auth_headers)).json()

        assert corps["total"] == 1
        assert corps["tronque"] is False
        assert [v["id"] for v in corps["items"]] == [ouverte.json()["id"]]


class TestBootstrap:
    async def test_base_neuve_puis_relance_idempotente(self, engine, monkeypatch):
        from app import bootstrap as module

        fabrique = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(module, "SessionLocal", fabrique)

        organisation, creee, motifs = await module.bootstrap(
            code="min-test", nom="Ministère de Test"
        )
        _, recreee, remotifs = await module.bootstrap(code="MIN-TEST", nom="Ministère de Test")

        assert (organisation.code, creee, motifs) == ("MIN-TEST", True, 8)
        assert (recreee, remotifs) == (False, 0)
        async with fabrique() as session:
            assert len((await session.execute(select(Organization))).scalars().all()) == 1

    async def test_code_invalide(self, engine, monkeypatch):
        from app import bootstrap as module

        monkeypatch.setattr(
            module, "SessionLocal", async_sessionmaker(bind=engine, expire_on_commit=False)
        )

        try:
            await module.bootstrap(code="code avec espaces", nom="X")
        except ValueError:
            return
        raise AssertionError("Un code invalide aurait dû être refusé.")
