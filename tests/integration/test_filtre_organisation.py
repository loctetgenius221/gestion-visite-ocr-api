"""Le paramètre `organization_id` des routes de lecture, et le refus de l'inconnu.

Deux garanties, nées du même incident : `GET /visits?organization_id=<X>` répondait
`200` avec `total: 3`, exactement comme sans filtre. Le paramètre n'était déclaré
nulle part, FastAPI l'avait donc jeté en silence, et le dashboard affichait le
registre de la plateforme en croyant montrer celui d'une structure.

Les tests ci-dessous fixent les deux moitiés de la réparation :

- le filtre existe et **filtre**, sur le registre, l'export, les analytiques, les
  comptes et les référentiels — un total identique entre appel filtré et appel nu
  était le symptôme d'origine, il est vérifié comme tel ;
- un paramètre que la route ne connaît pas est **refusé**, pas ignoré. C'est ce
  qui aurait signalé le problème le premier jour plutôt qu'au troisième écran.

Le filtre reste par ailleurs soumis au périmètre : le passer n'est pas un moyen
d'en changer, seul l'ADMIN de plateforme a de quoi le faire varier.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.enums import CheckoutOrigin, DocumentType, VisitStatus
from app.models.visit import Visit
from app.models.visitor import Visitor


@pytest.fixture
async def visite_locale(session: AsyncSession, seeded: dict) -> Visit:
    """Une visite dans l'organisation du jeu de test, en regard de celle de `voisine`.

    Sans elle, un filtre cassé et un filtre correct rendraient le même résultat :
    il faut deux structures peuplées pour que la différence soit observable.
    """
    visiteur = Visitor(
        prenom="Moussa",
        nom="Faye",
        type_document=DocumentType.CNI,
        numero_document="MIN0000001",
    )
    session.add(visiteur)
    await session.flush()

    visite = Visit(
        organization_id=seeded["organization"].id,
        visitor_id=visiteur.id,
        service_id=seeded["service"].id,
        agent_id=seeded["agent"].id,
        checked_in_by=seeded["user"].id,
    )
    session.add(visite)
    await session.commit()
    return visite


class TestRegistreFiltrable:
    """Le symptôme rapporté par le front : un filtre sans effet, et muet."""

    async def test_ladmin_filtre_le_registre_sur_une_organisation(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict, visite_locale
    ) -> None:
        sans_filtre = await client.get("/visits", headers=admin_headers)
        assert sans_filtre.json()["total"] == 2

        filtre = await client.get(
            f"/visits?organization_id={voisine['organization'].id}", headers=admin_headers
        )

        assert filtre.status_code == 200, filtre.text
        # Le cœur du test : un total qui ne bouge pas, c'est le bug d'origine.
        assert filtre.json()["total"] == 1
        assert filtre.json()["items"][0]["id"] == str(voisine["visit"].id)

    async def test_lexport_suit_le_meme_filtre(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict, visite_locale
    ) -> None:
        reponse = await client.get(
            f"/visits/export?organization_id={voisine['organization'].id}",
            headers=admin_headers,
        )

        assert reponse.status_code == 200, reponse.text
        contenu = reponse.content.decode("utf-8-sig")
        assert "Ndoye" in contenu
        assert "Faye" not in contenu

    async def test_les_clotures_automatiques_se_filtrent_aussi(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        session: AsyncSession,
        voisine: dict,
        visite_locale: Visit,
    ) -> None:
        for visite in (voisine["visit"], visite_locale):
            visite.statut = VisitStatus.SORTI
            visite.checked_out_at = utcnow()
            visite.checkout_origin = CheckoutOrigin.AUTOMATIC
        await session.commit()

        toutes = await client.get("/visits/auto-closed", headers=admin_headers)
        une_seule = await client.get(
            f"/visits/auto-closed?organization_id={voisine['organization'].id}",
            headers=admin_headers,
        )

        assert une_seule.status_code == 200, une_seule.text
        assert toutes.json()["total"] == 2
        assert une_seule.json()["total"] == 1


class TestAnalytiquesFiltrables:
    """Les sept routes du tableau de bord, sans quoi il reste un compteur plateforme."""

    ROUTES_ANALYTIQUES = [
        "/dashboard/stats",
        "/dashboard/stats/timeseries",
        "/dashboard/stats/by-service",
        "/dashboard/stats/by-purpose",
        "/dashboard/stats/peak-hours",
        "/dashboard/stats/avg-duration",
        "/dashboard/stats/top-agents",
    ]

    @pytest.mark.parametrize("route", ROUTES_ANALYTIQUES)
    async def test_chaque_route_accepte_le_filtre(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict, route: str
    ) -> None:
        reponse = await client.get(
            f"{route}?organization_id={voisine['organization'].id}", headers=admin_headers
        )

        assert reponse.status_code == 200, reponse.text

    async def test_les_compteurs_suivent_lorganisation_demandee(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict, visite_locale
    ) -> None:
        plateforme = await client.get("/dashboard/stats", headers=admin_headers)
        structure = await client.get(
            f"/dashboard/stats?organization_id={voisine['organization'].id}",
            headers=admin_headers,
        )

        assert plateforme.json()["presents_actuellement"] == 2
        assert structure.json()["presents_actuellement"] == 1

    async def test_le_top_agents_ne_melange_pas_les_annuaires(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict, visite_locale
    ) -> None:
        reponse = await client.get(
            f"/dashboard/stats/top-agents?organization_id={voisine['organization'].id}",
            headers=admin_headers,
        )

        assert [entree["agent_name"] for entree in reponse.json()["items"]] == ["Ousmane Sarr"]

    async def test_la_repartition_par_service_reste_dans_la_structure(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict, visite_locale
    ) -> None:
        reponse = await client.get(
            f"/dashboard/stats/by-service?organization_id={voisine['organization'].id}",
            headers=admin_headers,
        )

        assert reponse.json()["total"] == 1
        assert [item["label"] for item in reponse.json()["items"]] == ["Capitainerie"]


class TestComptesEtReferentielsFiltrables:
    async def test_les_comptes_se_filtrent_par_organisation(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get(
            f"/users?organization_id={voisine['organization'].id}", headers=admin_headers
        )

        assert reponse.status_code == 200, reponse.text
        assert [u["identifiant"] for u in reponse.json()["items"]] == ["agent-pad"]

    async def test_les_services_se_filtrent_par_organisation(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get(
            f"/services?organization_id={voisine['organization'].id}", headers=admin_headers
        )

        assert [s["code"] for s in reponse.json()] == ["CAP"]

    async def test_les_agents_se_filtrent_par_organisation(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get(
            f"/agents?organization_id={voisine['organization'].id}", headers=admin_headers
        )

        assert [a["name"] for a in reponse.json()] == ["Ousmane Sarr"]

    async def test_les_motifs_se_filtrent_par_organisation(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict
    ) -> None:
        """Filtré, l'ADMIN voit ce que verrait la structure : ses motifs, plus les communs."""
        reponse = await client.get(
            f"/purposes?organization_id={voisine['organization'].id}", headers=admin_headers
        )

        assert [p["libelle"] for p in reponse.json()] == ["Escale navire"]

    async def test_un_agent_porte_son_organisation(
        self, client: AsyncClient, auth_headers: dict[str, str], seeded: dict
    ) -> None:
        """`organization_id` n'est pas une colonne d'`agents` : il vient de son service."""
        reponse = await client.get("/agents", headers=auth_headers)

        assert reponse.status_code == 200, reponse.text
        assert {a["organization_id"] for a in reponse.json()} == {str(seeded["organization"].id)}


class TestLeFiltreNeContournePasLePerimetre:
    """Un filtre n'est pas une permission : `TenantScope` reste seul juge."""

    async def test_demander_une_autre_organisation_est_refuse(
        self, client: AsyncClient, org_admin_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get(
            f"/visits?organization_id={voisine['organization'].id}", headers=org_admin_headers
        )

        assert reponse.status_code == 403
        assert reponse.json()["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"

    async def test_demander_la_sienne_est_accepte(
        self, client: AsyncClient, org_admin_headers: dict[str, str], seeded: dict
    ) -> None:
        """Le dashboard envoie le même filtre quel que soit le rôle : il doit passer."""
        reponse = await client.get(
            f"/visits?organization_id={seeded['organization'].id}", headers=org_admin_headers
        )

        assert reponse.status_code == 200, reponse.text

    async def test_le_refus_vaut_aussi_pour_les_analytiques(
        self, client: AsyncClient, org_admin_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get(
            f"/dashboard/stats?organization_id={voisine['organization'].id}",
            headers=org_admin_headers,
        )

        assert reponse.status_code == 403
        assert reponse.json()["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"


class TestParametreInconnuRefuse:
    """Ignorer un paramètre, c'est répondre à une autre question que celle posée.

    Le rejet vaut 400 `VALIDATION_ERROR` et non 422 : la spec §5.6 réserve le 422
    aux échecs **métier**, et une query string fautive n'en est pas un. C'est le
    code que rendent déjà `?page=0` ou `?statut=inconnu`, ce qui laisse au front
    un seul cas à traiter plutôt que deux.
    """

    async def test_un_parametre_inconnu_est_rejete(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        reponse = await client.get("/visits?organisation_id=42", headers=admin_headers)

        assert reponse.status_code == 400
        assert reponse.json()["error_code"] == "VALIDATION_ERROR"

    async def test_le_message_nomme_le_fautif_et_liste_lattendu(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """De quoi distinguer la faute de frappe du filtre pas encore implémenté."""
        reponse = await client.get("/visits?statuts=PRESENT", headers=admin_headers)

        detail = reponse.json()["details"][0]
        assert detail["loc"] == ["query", "statuts"]
        assert "organization_id" in detail["ctx"]["parametres_acceptes"]

    async def test_les_parametres_declares_passent_toujours(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        """Y compris ceux connus sous un alias — `status`, `date`, `format`."""
        for chemin in (
            "/visits?statut=PRESENT&page=1&sort=asc",
            "/users?status=active",
            "/visits/export?format=csv",
            "/visits/auto-closed?date=2026-01-01",
            "/services?include_archived=true",
        ):
            reponse = await client.get(chemin, headers=admin_headers)
            assert reponse.status_code == 200, f"{chemin} : {reponse.text}"

    async def test_le_refus_vaut_sur_toutes_les_routes(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        for route in ("/agents", "/services", "/purposes", "/dashboard/stats", "/users"):
            reponse = await client.get(f"{route}?inconnu=1", headers=admin_headers)
            assert reponse.status_code == 400, f"{route} a ignoré un paramètre inconnu"
