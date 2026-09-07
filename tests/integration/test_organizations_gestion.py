"""Gestion d'une organisation depuis le dashboard : statut, administrateurs, logo.

Ces routes sont celles qu'un administrateur manipule réellement à l'écran. Deux
d'entre elles portent une garantie qui ne se voit pas dans la signature :

- `PATCH /status` refuse la désactivation tant que des visiteurs sont présents.
  Couper l'accès à ce moment-là laisserait des visites ouvertes qu'aucun agent ne
  pourrait plus clôturer ;
- `POST /logo` décide du format d'après le **contenu** du fichier. L'ancienne
  validation ne lisait que l'extension du nom : un document HTML renommé
  `logo.png` était stocké, puis servi depuis l'origine du dashboard.
"""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.visit import Visit
from app.models.visitor import Visitor
from tests.conftest import TEST_PASSWORD


def png(taille: tuple[int, int] = (64, 64), couleur: str = "navy") -> bytes:
    tampon = io.BytesIO()
    Image.new("RGBA", taille, couleur).save(tampon, format="PNG")
    return tampon.getvalue()


class TestActivation:
    async def test_desactiver_coupe_lacces_des_comptes_rattaches(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        org_admin_headers: dict[str, str],
        organization: Organization,
    ) -> None:
        """Effet immédiat, sessions déjà ouvertes comprises.

        Le jeton de l'administrateur d'organisation reste valide et non expiré :
        s'il suffisait à passer, la désactivation ne prendrait effet qu'au bout de
        la durée de vie des sessions.
        """
        assert (await client.get("/services", headers=org_admin_headers)).status_code == 200

        desactivation = await client.patch(
            f"/organizations/{organization.id}/status",
            headers=admin_headers,
            json={"is_active": False, "motif": "Fin de convention"},
        )
        assert desactivation.status_code == 200, desactivation.text
        assert desactivation.json()["is_active"] is False

        refus = await client.get("/services", headers=org_admin_headers)
        assert refus.status_code == 403
        assert refus.json()["error_code"] == "INACTIVE_ORGANIZATION"

    async def test_la_connexion_est_refusee_pendant_la_desactivation(
        self, client: AsyncClient, admin_headers: dict[str, str], organization: Organization, seeded
    ) -> None:
        await client.patch(
            f"/organizations/{organization.id}/status",
            headers=admin_headers,
            json={"is_active": False},
        )

        connexion = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )

        assert connexion.status_code == 403
        assert connexion.json()["error_code"] == "INACTIVE_ORGANIZATION"

    async def test_reactiver_rend_lacces_tel_quil_etait(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        organization: Organization,
        seeded,
    ) -> None:
        """Aucun compte n'a été touché : le rattachement suffit à tout rétablir."""
        for actif in (False, True):
            reponse = await client.patch(
                f"/organizations/{organization.id}/status",
                headers=admin_headers,
                json={"is_active": actif},
            )
            assert reponse.status_code == 200

        connexion = await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": TEST_PASSWORD}
        )
        assert connexion.status_code == 200

    async def test_desactiver_est_refuse_si_des_visiteurs_sont_presents(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        session: AsyncSession,
        organization: Organization,
        seeded: dict,
    ) -> None:
        visiteur = Visitor(
            prenom="Awa", nom="Diop", type_document="CNI", numero_document="1112223330"
        )
        session.add(visiteur)
        await session.flush()
        session.add(
            Visit(
                organization_id=organization.id,
                visitor_id=visiteur.id,
                service_id=seeded["service"].id,  # type: ignore[union-attr]
                checked_in_by=seeded["user"].id,  # type: ignore[union-attr]
            )
        )
        await session.commit()

        reponse = await client.patch(
            f"/organizations/{organization.id}/status",
            headers=admin_headers,
            json={"is_active": False},
        )

        assert reponse.status_code == 409
        assert reponse.json()["error_code"] == "ORGANIZATION_HAS_ACTIVE_VISITS"
        assert reponse.json()["details"]["visites_en_cours"] == 1

    async def test_un_org_admin_ne_peut_pas_activer_sa_propre_structure(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        """Sinon il pourrait se réactiver seul après une suspension."""
        reponse = await client.patch(
            f"/organizations/{organization.id}/status",
            headers=org_admin_headers,
            json={"is_active": False},
        )

        assert reponse.status_code == 403

    async def test_lactivation_est_tracee_avec_son_motif(
        self, client: AsyncClient, admin_headers: dict[str, str], organization: Organization
    ) -> None:
        await client.patch(
            f"/organizations/{organization.id}/status",
            headers=admin_headers,
            json={"is_active": False, "motif": "Suspension administrative"},
        )

        journal = await client.get(
            "/audit-logs?action=organization.status_changed", headers=admin_headers
        )
        entree = journal.json()["items"][0]

        assert entree["metadata"]["is_active"] is False
        assert entree["metadata"]["motif"] == "Suspension administrative"
        assert entree["organization_id"] == str(organization.id)


class TestAdministrateurs:
    async def test_nommer_un_administrateur_apres_coup(
        self, client: AsyncClient, admin_headers: dict[str, str], organization: Organization
    ) -> None:
        """La création d'organisation n'est plus la seule occasion d'en nommer un."""
        reponse = await client.post(
            f"/organizations/{organization.id}/administrators",
            headers=admin_headers,
            json={"nom": "Awa Ndiaye", "identifiant": "orgadmin042"},
        )

        assert reponse.status_code == 201, reponse.text
        corps = reponse.json()
        assert corps["user"]["role"] == "ORG_ADMIN"
        assert corps["user"]["organization_id"] == str(organization.id)

        connexion = await client.post(
            "/auth/login",
            json={"identifiant": "orgadmin042", "mot_de_passe": corps["mot_de_passe"]},
        )
        assert connexion.status_code == 200

    async def test_un_org_admin_peut_nommer_un_pair_dans_sa_structure(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        reponse = await client.post(
            f"/organizations/{organization.id}/administrators",
            headers=org_admin_headers,
            json={"nom": "Second Admin", "identifiant": "orgadmin043"},
        )

        assert reponse.status_code == 201, reponse.text

    async def test_un_org_admin_ne_peut_pas_en_nommer_ailleurs(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        other_organization: Organization,
    ) -> None:
        reponse = await client.post(
            f"/organizations/{other_organization.id}/administrators",
            headers=org_admin_headers,
            json={"nom": "Intrus", "identifiant": "intrus001"},
        )

        assert reponse.status_code == 403
        assert reponse.json()["error_code"] == "CROSS_ORGANIZATION_FORBIDDEN"

    async def test_lister_les_administrateurs(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        organization: Organization,
        org_admin,
    ) -> None:
        reponse = await client.get(
            f"/organizations/{organization.id}/administrators", headers=admin_headers
        )

        assert reponse.status_code == 200
        assert [c["identifiant"] for c in reponse.json()] == ["orgadmin001"]

    async def test_lister_les_comptes_de_la_structure(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        organization: Organization,
        seeded: dict,
    ) -> None:
        reponse = await client.get(
            f"/organizations/{organization.id}/users", headers=org_admin_headers
        )

        assert reponse.status_code == 200
        identifiants = {c["identifiant"] for c in reponse.json()["items"]}
        assert {"agent001", "orgadmin001"} <= identifiants

    async def test_le_filtre_par_role_sapplique(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        organization: Organization,
        seeded: dict,
    ) -> None:
        reponse = await client.get(
            f"/organizations/{organization.id}/users?role=AGENT_CONTROLE",
            headers=org_admin_headers,
        )

        assert [c["identifiant"] for c in reponse.json()["items"]] == ["agent001"]

    async def test_une_organisation_inconnue_repond_404(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Et non une page vide, qui laisserait croire à une structure sans compte."""
        reponse = await client.get(
            "/organizations/00000000-0000-0000-0000-0000000000ff/users", headers=admin_headers
        )

        assert reponse.status_code == 404

    async def test_le_dernier_administrateur_ne_peut_pas_etre_desactive(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        org_admin,
    ) -> None:
        """Sans ce garde, l'organisation resterait sans personne pour la gérer.

        Le contrôle existait pour `ADMIN` mais ne s'appliquait jamais à
        `ORG_ADMIN` : sa condition d'entrée excluait le rôle qu'elle prétendait
        protéger, et les deux branches internes étaient mortes.
        """
        reponse = await client.patch(
            f"/users/{org_admin.id}/status", headers=admin_headers, json={"status": "inactive"}
        )

        assert reponse.status_code == 409
        assert reponse.json()["error_code"] == "LAST_ADMIN"

    async def test_le_dernier_administrateur_ne_peut_pas_etre_retrograde(
        self, client: AsyncClient, admin_headers: dict[str, str], org_admin
    ) -> None:
        reponse = await client.put(
            f"/users/{org_admin.id}", headers=admin_headers, json={"role": "AGENT_CONTROLE"}
        )

        assert reponse.status_code == 409
        assert reponse.json()["error_code"] == "LAST_ADMIN"

    async def test_avec_un_second_administrateur_la_desactivation_passe(
        self, client: AsyncClient, admin_headers: dict[str, str], organization, org_admin
    ) -> None:
        await client.post(
            f"/organizations/{organization.id}/administrators",
            headers=admin_headers,
            json={"nom": "Relève", "identifiant": "orgadmin044"},
        )

        reponse = await client.patch(
            f"/users/{org_admin.id}/status", headers=admin_headers, json={"status": "inactive"}
        )

        assert reponse.status_code == 200, reponse.text


class TestLogo:
    async def test_depot_et_normalisation(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        reponse = await client.post(
            f"/organizations/{organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("logo.png", png(), "image/png")},
        )

        assert reponse.status_code == 201, reponse.text
        url = reponse.json()["logo_url"]
        assert url is not None
        # Rangé sous l'organisation, jamais avec les pièces d'identité : la purge
        # des images périmées balaie `documents/`, elle effacerait le logo.
        assert f"organizations/{organization.id}" in url
        assert url.endswith(".png")

    async def test_un_fichier_qui_nest_pas_une_image_est_refuse(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        """Le nom dit « png », le contenu dit HTML : c'est le contenu qui tranche.

        Servi depuis l'origine du dashboard, ce fichier s'y exécuterait avec la
        session de l'administrateur qui l'ouvre.
        """
        piege = b"<html><script>alert(document.cookie)</script></html>"

        reponse = await client.post(
            f"/organizations/{organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("logo.png", piege, "image/png")},
        )

        assert reponse.status_code == 400
        assert reponse.json()["error_code"] == "UNSUPPORTED_IMAGE"

    async def test_une_image_trop_lourde_est_refusee(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        enorme = b"\x89PNG\r\n\x1a\n" + b"\x00" * (3 * 1024 * 1024)

        reponse = await client.post(
            f"/organizations/{organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("logo.png", enorme, "image/png")},
        )

        assert reponse.status_code == 400
        assert reponse.json()["error_code"] == "FILE_TOO_LARGE"

    async def test_une_grande_image_est_ramenee_a_la_taille_prevue(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        """Le dashboard la redimensionnerait de toute façon, en payant la bande passante."""
        from app.core.config import settings

        reponse = await client.post(
            f"/organizations/{organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("grand.png", png((2000, 1200)), "image/png")},
        )
        assert reponse.status_code == 201, reponse.text

        chemin = reponse.json()["logo_url"].removeprefix(settings.STORAGE_PUBLIC_BASE_URL)
        fichier = f"{settings.STORAGE_DIR}{chemin}"
        with Image.open(fichier) as image:
            assert max(image.size) == settings.LOGO_MAX_DIMENSION_PX
            assert image.format == "PNG"

    async def test_un_second_depot_remplace_le_premier(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        """L'URL change à chaque dépôt : réécrire le même chemin laisserait les
        caches servir l'ancien logo pendant des heures."""
        premier = await client.post(
            f"/organizations/{organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("a.png", png(couleur="red"), "image/png")},
        )
        second = await client.post(
            f"/organizations/{organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("b.png", png(couleur="green"), "image/png")},
        )

        assert second.status_code == 201
        assert second.json()["logo_url"] != premier.json()["logo_url"]

    async def test_retirer_le_logo(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        await client.post(
            f"/organizations/{organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("logo.png", png(), "image/png")},
        )

        reponse = await client.delete(
            f"/organizations/{organization.id}/logo", headers=org_admin_headers
        )

        assert reponse.status_code == 200
        assert reponse.json()["logo_url"] is None

    async def test_logo_url_nest_pas_modifiable_en_clair(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        """Sinon un administrateur ferait pointer chaque écran vers un traceur tiers."""
        reponse = await client.put(
            f"/organizations/{organization.id}",
            headers=org_admin_headers,
            json={"logo_url": "https://attaquant.example/pixel.png"},
        )

        assert reponse.status_code == 200
        assert reponse.json()["logo_url"] is None

    async def test_un_org_admin_ne_depose_pas_de_logo_ailleurs(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        other_organization: Organization,
    ) -> None:
        reponse = await client.post(
            f"/organizations/{other_organization.id}/logo",
            headers=org_admin_headers,
            files={"logo": ("logo.png", png(), "image/png")},
        )

        assert reponse.status_code == 403


class TestCharteGraphique:
    async def test_la_charte_est_lisible_sans_authentification(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        organization: Organization,
    ) -> None:
        """La page de connexion s'affiche avant tout token : elle en a besoin."""
        await client.put(
            f"/organizations/{organization.id}/theme",
            headers=org_admin_headers,
            json={"primary_color": "#1E3A8A", "accent_color": "#F59E0B"},
        )

        reponse = await client.get(f"/organizations/theme?code={organization.code}")

        assert reponse.status_code == 200, reponse.text
        corps = reponse.json()
        assert corps["primary_color"] == "#1E3A8A"
        assert corps["nom"] == organization.nom
        # Rien d'autre que la devanture : ni contact, ni état d'activation.
        assert "contact_email" not in corps
        assert "is_active" not in corps

    async def test_une_structure_desactivee_na_plus_de_devanture(
        self, client: AsyncClient, admin_headers: dict[str, str], organization: Organization
    ) -> None:
        await client.patch(
            f"/organizations/{organization.id}/status",
            headers=admin_headers,
            json={"is_active": False},
        )

        reponse = await client.get(f"/organizations/theme?code={organization.code}")

        assert reponse.status_code == 404

    async def test_une_couleur_invalide_est_refusee(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        reponse = await client.put(
            f"/organizations/{organization.id}/theme",
            headers=org_admin_headers,
            json={"primary_color": "bleu marine"},
        )

        assert reponse.status_code == 400

    @pytest.mark.parametrize("champ", ["localisation", "description", "accent_color"])
    async def test_un_null_explicite_efface_le_champ(
        self,
        client: AsyncClient,
        org_admin_headers: dict[str, str],
        organization: Organization,
        champ: str,
    ) -> None:
        """Sans la distinction absent/null, aucun champ facultatif n'était effaçable.

        Le dashboard doit pouvoir retirer une localisation ou une couleur, pas
        seulement la remplacer.
        """
        valeurs = {
            "localisation": "Dakar",
            "description": "Structure de test",
            "accent_color": "#F59E0B",
        }
        rempli = await client.put(
            f"/organizations/{organization.id}",
            headers=org_admin_headers,
            json={champ: valeurs[champ]},
        )
        assert rempli.json()[champ] == valeurs[champ]

        vide = await client.put(
            f"/organizations/{organization.id}",
            headers=org_admin_headers,
            json={champ: None},
        )

        assert vide.status_code == 200, vide.text
        assert vide.json()[champ] is None

    async def test_un_champ_absent_nest_pas_touche(
        self, client: AsyncClient, org_admin_headers: dict[str, str], organization: Organization
    ) -> None:
        await client.put(
            f"/organizations/{organization.id}",
            headers=org_admin_headers,
            json={"localisation": "Dakar"},
        )

        reponse = await client.put(
            f"/organizations/{organization.id}",
            headers=org_admin_headers,
            json={"description": "Ajoutée après coup"},
        )

        assert reponse.json()["localisation"] == "Dakar"


class TestCreationAtomique:
    async def test_un_identifiant_dadmin_deja_pris_annule_toute_la_creation(
        self, client: AsyncClient, admin_headers: dict[str, str], seeded: dict
    ) -> None:
        """Organisation et premier administrateur naissent ensemble, ou pas du tout.

        Sans cette atomicité, un identifiant déjà pris laisserait une structure
        créée mais sans personne pour l'administrer — visible dans la liste, et
        impossible à prendre en main autrement qu'en y nommant quelqu'un à la main.
        """
        reponse = await client.post(
            "/organizations",
            headers=admin_headers,
            json={
                "nom": "Structure fantôme",
                "code": "FANTOME",
                "admin_nom": "Doublon",
                "admin_identifiant": "agent001",
            },
        )

        assert reponse.status_code == 409
        assert reponse.json()["error_code"] == "DUPLICATE_IDENTIFIANT"

        liste = await client.get("/organizations?search=FANTOME", headers=admin_headers)
        assert liste.json()["total"] == 0

    async def test_un_admin_sans_mot_de_passe_en_recoit_un_utilisable(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        creation = await client.post(
            "/organizations",
            headers=admin_headers,
            json={
                "nom": "Agence de Régulation",
                "code": "ARTP",
                "admin_nom": "Directrice",
                "admin_identifiant": "admin.artp",
            },
        )
        assert creation.status_code == 201, creation.text
        corps = creation.json()

        connexion = await client.post(
            "/auth/login",
            json={
                "identifiant": "admin.artp",
                "mot_de_passe": corps["admin_temporary_password"],
            },
        )

        assert connexion.status_code == 200
        assert connexion.json()["user"]["role"] == "ORG_ADMIN"
