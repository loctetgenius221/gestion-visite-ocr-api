"""Cloisonnement entre organisations : aucune donnée ne franchit la frontière.

Ces tests décrivent la garantie centrale du mode multi-structures. Chacun
correspond à une fuite réelle, constatée avant que le périmètre ne soit résolu
en un seul endroit (`TenantScope`) :

- la recherche de visiteurs exposait l'identité et le numéro de pièce des
  visiteurs de **toutes** les structures à n'importe quel agent d'accueil ;
- les listes de services et d'agents n'étaient cloisonnées que pour `ORG_ADMIN`
  et `SUPERVISEUR` : l'agent de contrôle, destinataire principal de ces listes,
  recevait l'annuaire complet du parc ;
- le journal d'audit et les réglages système, ouverts à `ORG_ADMIN`, étaient
  globaux — activité et politique de sécurité des autres structures comprises.

Le fil conducteur est le même partout : deux organisations, chacune avec ses
comptes et ses données, et la vérification qu'aucune ne voit l'autre.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.organization import Organization
from app.models.referentiel import Agent, Purpose, Service
from app.models.user import User
from app.models.visit import Visit
from app.models.visitor import Visitor
from tests.conftest import TEST_PASSWORD


@pytest.fixture
async def voisine(
    session: AsyncSession, other_organization: Organization, seeded: dict
) -> dict[str, object]:
    """Seconde structure complète : un agent, un service, une personne, une visite.

    Elle n'a **aucun** lien avec l'organisation du jeu de test : tout ce que les
    tests ci-dessous y trouvent est, par construction, une fuite.
    """
    agent_compte = User(
        nom="Agent du Port",
        identifiant="agent-pad",
        mot_de_passe_hash=hash_password(TEST_PASSWORD),
        role=UserRole.AGENT_CONTROLE,
        organization_id=other_organization.id,
        is_active=True,
    )
    service = Service(
        name="Capitainerie", code="CAP", organization_id=other_organization.id
    )
    motif = Purpose(libelle="Escale navire", organization_id=other_organization.id)
    session.add_all([agent_compte, service, motif])
    await session.flush()

    agent = Agent(name="Ousmane Sarr", role="Capitaine", service_id=service.id)
    visiteur = Visitor(
        prenom="Khady",
        nom="Ndoye",
        type_document="CNI",
        numero_document="PAD0000001",
    )
    session.add_all([agent, visiteur])
    await session.flush()

    visite = Visit(
        organization_id=other_organization.id,
        visitor_id=visiteur.id,
        service_id=service.id,
        agent_id=agent.id,
        checked_in_by=agent_compte.id,
    )
    session.add(visite)
    await session.commit()

    return {
        "organization": other_organization,
        "user": agent_compte,
        "service": service,
        "agent": agent,
        "purpose": motif,
        "visitor": visiteur,
        "visit": visite,
    }


@pytest.fixture
async def voisine_headers(client: AsyncClient, voisine: dict) -> dict[str, str]:
    reponse = await client.post(
        "/auth/login", json={"identifiant": "agent-pad", "mot_de_passe": TEST_PASSWORD}
    )
    assert reponse.status_code == 200, reponse.text
    return {"Authorization": f"Bearer {reponse.json()['access_token']}"}


class TestRechercheDeVisiteurs:
    """La fiche visiteur est commune, sa consultation ne l'est pas.

    Une même personne peut se présenter dans plusieurs structures : lui créer une
    fiche par organisation dupliquerait son identité — donc son numéro de pièce et
    son NIN — autant de fois. Le cloisonnement porte donc sur le lien de visite :
    on ne retrouve quelqu'un que s'il est déjà venu chez soi.
    """

    async def test_un_agent_ne_trouve_pas_les_visiteurs_dune_autre_structure(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get("/visitors?search=Ndoye", headers=auth_headers)

        assert reponse.status_code == 200
        assert reponse.json()["items"] == []
        assert reponse.json()["total"] == 0

    async def test_le_numero_de_piece_dun_tiers_ne_remonte_pas_non_plus(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        """Rechercher par numéro de document est le contournement le plus direct."""
        reponse = await client.get("/visitors?search=PAD0000001", headers=auth_headers)

        assert reponse.json()["items"] == []

    async def test_chaque_structure_retrouve_les_siens(
        self, client: AsyncClient, voisine_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get("/visitors?search=Ndoye", headers=voisine_headers)

        assert [v["nom"] for v in reponse.json()["items"]] == ["Ndoye"]

    async def test_ladministrateur_de_plateforme_voit_tout(
        self, client: AsyncClient, admin_headers: dict[str, str], voisine: dict
    ) -> None:
        """Il n'est rattaché à aucune structure : c'est ce qui le définit."""
        reponse = await client.get("/visitors?search=Ndoye", headers=admin_headers)

        assert [v["nom"] for v in reponse.json()["items"]] == ["Ndoye"]


class TestReferentiels:
    async def test_un_agent_ne_voit_que_les_services_de_sa_structure(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        """L'agent de contrôle était le seul rôle non cloisonné sur cette route."""
        codes = {s["code"] for s in (await client.get("/services", headers=auth_headers)).json()}

        assert "CAP" not in codes
        assert "DRH" in codes

    async def test_un_agent_ne_voit_que_lannuaire_de_sa_structure(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        noms = {a["name"] for a in (await client.get("/agents", headers=auth_headers)).json()}

        assert "Ousmane Sarr" not in noms
        assert "Aminata Diallo" in noms

    async def test_les_agents_dun_service_etranger_sont_refuses(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        service_id = voisine["service"].id  # type: ignore[union-attr]

        reponse = await client.get(f"/services/{service_id}/agents", headers=auth_headers)

        assert reponse.status_code == 403
        assert reponse.json()["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"

    async def test_les_motifs_dune_autre_structure_ne_sont_pas_proposes(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        motifs = (await client.get("/purposes", headers=auth_headers)).json()
        libelles = {p["libelle"] for p in motifs}

        assert "Escale navire" not in libelles
        assert "Rendez-vous professionnel" in libelles

    async def test_un_motif_commun_est_propose_a_toutes(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        voisine_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        """`organization_id` nul = motif de plateforme, visible partout."""
        creation = await client.post(
            "/purposes", headers=admin_headers, json={"libelle": "Contrôle réglementaire"}
        )
        assert creation.status_code == 201
        assert creation.json()["organization_id"] is None

        for headers in (auth_headers, voisine_headers):
            libelles = {
                p["libelle"] for p in (await client.get("/purposes", headers=headers)).json()
            }
            assert "Contrôle réglementaire" in libelles

    async def test_deux_structures_peuvent_avoir_le_meme_code_de_service(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        voisine: dict,
        admin_headers: dict[str, str],
    ) -> None:
        """« DRH » existe dans chaque ministère : l'unicité est locale, pas globale."""
        reponse = await client.post(
            "/services",
            headers=admin_headers,
            json={
                "name": "Ressources Humaines du Port",
                "code": "DRH",
                "organization_id": str(voisine["organization"].id),  # type: ignore[union-attr]
            },
        )

        assert reponse.status_code == 201, reponse.text

    async def test_le_meme_code_deux_fois_dans_une_structure_est_refuse(
        self, client: AsyncClient, org_admin_headers: dict[str, str], seeded: dict
    ) -> None:
        reponse = await client.post(
            "/services", headers=org_admin_headers, json={"name": "Bis", "code": "DRH"}
        )

        assert reponse.status_code == 409
        assert reponse.json()["error_code"] == "DUPLICATE_REFERENTIEL"


class TestVisites:
    async def test_le_registre_dune_autre_structure_est_invisible(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        reponse = await client.get("/visits", headers=auth_headers)

        assert reponse.status_code == 200
        identifiants = {v["id"] for v in reponse.json()["items"]}
        assert str(voisine["visit"].id) not in identifiants  # type: ignore[union-attr]

    async def test_une_visite_etrangere_repond_403_en_lecture(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        visite_id = voisine["visit"].id  # type: ignore[union-attr]

        reponse = await client.get(f"/visits/{visite_id}", headers=auth_headers)

        assert reponse.status_code == 403

    async def test_cloturer_la_visite_dune_autre_structure_est_refuse(
        self, client: AsyncClient, auth_headers: dict[str, str], voisine: dict
    ) -> None:
        """L'écriture doit être cloisonnée aussi fermement que la lecture."""
        visite_id = voisine["visit"].id  # type: ignore[union-attr]

        reponse = await client.put(f"/visits/{visite_id}/checkout", headers=auth_headers)

        assert reponse.status_code == 403

    async def test_une_visite_ne_peut_pas_etre_creee_chez_le_voisin(
        self, client: AsyncClient, auth_headers: dict[str, str], seeded: dict, voisine: dict
    ) -> None:
        """`organization_id` vient du périmètre, jamais du corps de la requête."""
        reponse = await client.post(
            "/visits",
            headers=auth_headers,
            json={
                "organization_id": str(voisine["organization"].id),  # type: ignore[union-attr]
                "visitor": {
                    "prenom": "Awa",
                    "nom": "Diop",
                    "type_document": "CNI",
                    "numero_document": "9998887770",
                },
                "service_id": str(seeded["service"].id),  # type: ignore[union-attr]
                "purpose_id": str(seeded["purpose"].id),  # type: ignore[union-attr]
            },
        )

        assert reponse.status_code == 403
        assert reponse.json()["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"


class TestJournalDaudit:
    async def test_un_org_admin_ne_lit_que_le_journal_de_sa_structure(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        voisine_headers: dict[str, str],
        voisine: dict,
    ) -> None:
        """La connexion de l'agent voisin ne doit pas apparaître ici."""
        reponse = await client.get("/audit-logs", headers=org_admin_headers)

        assert reponse.status_code == 200
        identifiants = {
            entree["actor_identifiant"] for entree in reponse.json()["items"]
        }
        assert "agent-pad" not in identifiants

    async def test_un_org_admin_ne_peut_pas_viser_une_autre_structure(
        self, client: AsyncClient, org_admin_headers: dict[str, str], voisine: dict
    ) -> None:
        organisation_id = voisine["organization"].id  # type: ignore[union-attr]

        reponse = await client.get(
            f"/audit-logs?organization_id={organisation_id}", headers=org_admin_headers
        )

        assert reponse.status_code == 403
        assert reponse.json()["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"


class TestReglages:
    async def test_un_org_admin_ne_peut_pas_toucher_a_la_securite_de_la_plateforme(
        self, client: AsyncClient, org_admin_headers: dict[str, str]
    ) -> None:
        """Le verrouillage des comptes s'applique à toutes les structures."""
        reponse = await client.put(
            "/settings", headers=org_admin_headers, json={"max_failed_login_attempts": 20}
        )

        assert reponse.status_code == 403
        assert "max_failed_login_attempts" in reponse.json()["details"]["parametres_refuses"]

    async def test_un_org_admin_regle_sa_propre_heure_de_cloture(
        self, client: AsyncClient, org_admin_headers: dict[str, str]
    ) -> None:
        reponse = await client.put(
            "/settings",
            headers=org_admin_headers,
            json={"automatic_visit_closure_time": "20:00"},
        )

        assert reponse.status_code == 200, reponse.text
        assert reponse.json()["automatic_visit_closure_time"] == "20:00"
        assert reponse.json()["scope"] == "organization"
        assert reponse.json()["organization_overrides"] == ["automatic_visit_closure_time"]

    async def test_le_reglage_dune_structure_ne_deborde_pas_sur_lautre(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        admin_headers: dict[str, str],
        voisine: dict,
    ) -> None:
        await client.put(
            "/settings",
            headers=org_admin_headers,
            json={"automatic_visit_closure_time": "20:00"},
        )

        organisation_id = voisine["organization"].id  # type: ignore[union-attr]
        voisin = await client.get(
            f"/settings?organization_id={organisation_id}", headers=admin_headers
        )

        assert voisin.json()["automatic_visit_closure_time"] == "17:00"
        assert voisin.json()["organization_overrides"] == []

    async def test_retirer_la_surcharge_realigne_sur_la_plateforme(
        self, client: AsyncClient, org_admin_headers: dict[str, str], admin_headers: dict[str, str]
    ) -> None:
        """Réaligner n'est pas recopier : la structure suit de nouveau la plateforme."""
        await client.put(
            "/settings",
            headers=org_admin_headers,
            json={"automatic_visit_closure_time": "20:00"},
        )
        await client.put(
            "/settings", headers=admin_headers, json={"automatic_visit_closure_time": "18:30"}
        )

        reponse = await client.delete(
            "/settings/overrides/automatic_visit_closure_time", headers=org_admin_headers
        )

        assert reponse.status_code == 200, reponse.text
        assert reponse.json()["automatic_visit_closure_time"] == "18:30"
        assert reponse.json()["organization_overrides"] == []


class TestComptesNonRattaches:
    async def test_un_compte_sans_organisation_est_refuse_a_la_porte(
        self, client: AsyncClient, session: AsyncSession, seeded: dict
    ) -> None:
        """« Pas d'organisation » ne doit jamais valoir « toutes les organisations ».

        C'est cette ambiguïté qui ouvrait les données du parc entier aux comptes
        mal rattachés : le refus est explicite, et porte son propre code d'erreur.
        """
        orphelin = User(
            nom="Compte orphelin",
            identifiant="orphelin001",
            mot_de_passe_hash=hash_password(TEST_PASSWORD),
            role=UserRole.AGENT_CONTROLE,
            organization_id=None,
            is_active=True,
        )
        session.add(orphelin)
        await session.commit()

        connexion = await client.post(
            "/auth/login",
            json={"identifiant": "orphelin001", "mot_de_passe": TEST_PASSWORD},
        )
        assert connexion.status_code == 200
        headers = {"Authorization": f"Bearer {connexion.json()['access_token']}"}

        reponse = await client.get("/services", headers=headers)

        assert reponse.status_code == 403
        assert reponse.json()["error_code"] == "UNSCOPED_ACCOUNT"
