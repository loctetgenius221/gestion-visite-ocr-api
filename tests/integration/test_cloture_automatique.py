"""Clôture automatique des visites restées ouvertes.

Une visite est clôturée à la première heure limite qui suit son entrée, et à
cette heure-là. La tâche tourne toutes les cinq minutes et se rejoue sans effet
de bord : c'est ce qui rattrape les visites arrivées en retard par la synchro
hors-ligne (ADR-025).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automatic_closure_run import AutomaticClosureRun
from app.models.base import ensure_utc
from app.models.enums import CheckoutOrigin, VisitStatus
from app.models.forgotten_document import ForgottenDocumentCase
from app.models.visit import Visit
from app.services.auto_closure_service import AutoClosureService
from tests.integration.test_visits import visit_payload

# Un jour fixe dans le passé : les entrées ne doivent pas être « dans le futur »
# pour l'API, qui compare à l'heure réelle.
JOUR = (datetime.now(UTC) - timedelta(days=5)).date()


def a(heure: int, minute: int = 0, *, jour_suivant: bool = False) -> datetime:
    jour = JOUR + timedelta(days=1) if jour_suivant else JOUR
    return datetime.combine(jour, time(heure, minute), tzinfo=UTC)


async def _entree(
    client: AsyncClient, seeded: dict, headers: dict, numero: str, moment: datetime
) -> uuid.UUID:
    payload = visit_payload(seeded, checked_in_at=moment.isoformat())
    payload["visitor"]["numero_document"] = numero
    reponse = await client.post("/visits", json=payload, headers=headers)
    assert reponse.status_code == 201, reponse.text
    return uuid.UUID(reponse.json()["id"])


async def _cloturer(session: AsyncSession, seeded: dict, maintenant: datetime) -> dict:
    return await AutoClosureService(session).run_automatic_closure(
        closure_time_str="17:00",
        organization_id=seeded["organization"].id,
        now=maintenant,
    )


async def _visite(session: AsyncSession, visit_id: uuid.UUID) -> Visit:
    visite = await session.get(Visit, visit_id, populate_existing=True)
    assert visite is not None
    return visite


class TestHeureDeCloture:
    async def test_cloturee_a_l_heure_limite_et_non_a_l_execution(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        visite_id = await _entree(client, seeded, auth_headers, "CL01", a(10))

        resultat = await _cloturer(session, seeded, a(19, 42))

        assert resultat["status"] == "completed"
        visite = await _visite(session, visite_id)
        assert visite.statut is VisitStatus.SORTI
        assert visite.checkout_origin is CheckoutOrigin.AUTOMATIC
        assert ensure_utc(visite.checked_out_at) == a(17)

    async def test_entree_apres_la_limite_attend_le_lendemain(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        visite_id = await _entree(client, seeded, auth_headers, "CL02", a(18))

        await _cloturer(session, seeded, a(20))
        assert (await _visite(session, visite_id)).statut is VisitStatus.PRESENT

        await _cloturer(session, seeded, a(17, 5, jour_suivant=True))
        visite = await _visite(session, visite_id)
        assert ensure_utc(visite.checked_out_at) == a(17, jour_suivant=True)

    async def test_avant_l_heure_limite_rien_n_est_du(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        visite_id = await _entree(client, seeded, auth_headers, "CL03", a(9))

        resultat = await _cloturer(session, seeded, a(16, 59))

        assert resultat["status"] == "nothing_to_close"
        assert (await _visite(session, visite_id)).statut is VisitStatus.PRESENT


class TestRattrapage:
    async def test_visite_synchronisee_apres_le_passage_du_jour(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        """Le passage de 17 h a eu lieu ; une visite saisie hors-ligne à 9 h arrive
        à 18 h. Avant, elle restait présente jusqu'au lendemain 17 h."""
        premiere = await _entree(client, seeded, auth_headers, "RT01", a(8))
        await _cloturer(session, seeded, a(17, 5))
        assert (await _visite(session, premiere)).statut is VisitStatus.SORTI

        tardive = await _entree(client, seeded, auth_headers, "RT02", a(9))
        resultat = await _cloturer(session, seeded, a(18, 5))

        assert resultat["closed_visits_count"] == 1
        assert ensure_utc((await _visite(session, tardive)).checked_out_at) == a(17)
        runs = (await session.execute(select(AutomaticClosureRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].closed_visits_count == 2

    async def test_visite_de_la_veille_arrivee_ce_matin(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        visite_id = await _entree(client, seeded, auth_headers, "RT03", a(10))

        await _cloturer(session, seeded, a(9, jour_suivant=True))

        assert ensure_utc((await _visite(session, visite_id)).checked_out_at) == a(17)

    async def test_rejouer_ne_cree_ni_doublon_ni_fiche_en_plus(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        await _entree(client, seeded, auth_headers, "RT04", a(10))

        await _cloturer(session, seeded, a(17, 5))
        second = await _cloturer(session, seeded, a(17, 10))

        assert second["status"] == "nothing_to_close"
        fiches = (
            await session.execute(select(func.count()).select_from(ForgottenDocumentCase))
        ).scalar_one()
        assert fiches == 1


class TestPerimetre:
    async def test_simulation_ne_modifie_rien(
        self, client: AsyncClient, seeded: dict, auth_headers: dict, session: AsyncSession
    ):
        visite_id = await _entree(client, seeded, auth_headers, "PM01", a(10))

        resultat = await AutoClosureService(session).run_automatic_closure(
            organization_id=seeded["organization"].id, dry_run=True, now=a(18)
        )

        assert resultat == {
            "status": "dry_run",
            "run_date": JOUR.isoformat(),
            "visits_to_close": 1,
        }
        assert (await _visite(session, visite_id)).statut is VisitStatus.PRESENT

    async def test_les_visites_d_une_autre_organisation_ne_sont_pas_touchees(
        self, seeded: dict, session: AsyncSession, voisine: dict
    ):
        visite = voisine["visit"]
        visite.checked_in_at = a(10)
        await session.commit()

        await _cloturer(session, seeded, a(18))

        assert (await _visite(session, visite.id)).statut is VisitStatus.PRESENT
