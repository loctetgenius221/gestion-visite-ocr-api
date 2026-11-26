"""Preuves de l'audit du 26/11/2026 (voir audit/AUDIT_2026-11-26.md).

Chaque test décrit le comportement **attendu**. Tant que le constat n'est pas
corrigé, il est marqué `xfail(strict=True)` : la suite reste verte, et le jour où
le correctif arrive, le test passe, `strict` le signale, et il suffit de retirer
le marqueur pour en faire un test de non-régression.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# SEC-001 : fiche visiteur globale lue et réécrite depuis une autre organisation.
# Corrigé par la fiche par organisation (ADR-026) ; ces tests sont des tests de
# non-régression.


def _visite_par_saisie(service_id: str, numero: str) -> dict:
    """Enregistrement « saisie manuelle » d'une pièce dont on connaît le numéro."""
    return {
        "service_id": service_id,
        "motif_libre": "Rendez-vous",
        "visitor": {
            "prenom": "Faux",
            "nom": "Nom",
            "type_document": "CNI",
            "numero_document": numero,
        },
    }


async def test_sec001_numero_de_piece_ne_revele_pas_la_fiche_dune_autre_organisation(
    client: AsyncClient,
    session: AsyncSession,
    seeded: dict,
    voisine: dict,
    auth_headers: dict[str, str],
) -> None:
    """Un agent du ministère A ne doit rien apprendre d'une personne venue au port B."""
    visiteur = voisine["visitor"]
    visiteur.nin = "1751199000123"
    visiteur.telephone = "+221770000000"
    visiteur.email = "khady@example.org"
    await session.commit()

    reponse = await client.post(
        "/visits",
        json=_visite_par_saisie(str(seeded["service"].id), "PAD0000001"),
        headers=auth_headers,
    )

    assert reponse.status_code == 201, reponse.text
    fiche = reponse.json()["visitor"]
    # Aujourd'hui : NIN, téléphone et e-mail saisis par le port remontent à A.
    assert fiche.get("nin") is None
    assert fiche.get("telephone") is None
    assert fiche.get("email") is None


async def test_sec001_une_autre_organisation_ne_reecrit_pas_lidentite_du_registre(
    client: AsyncClient,
    session: AsyncSession,
    seeded: dict,
    voisine: dict,
    auth_headers: dict[str, str],
    voisine_headers: dict[str, str],
) -> None:
    """Le registre du port ne doit pas changer parce qu'un ministère a mal saisi un nom."""
    creation = await client.post(
        "/visits",
        json=_visite_par_saisie(str(seeded["service"].id), "PAD0000001"),
        headers=auth_headers,
    )
    assert creation.status_code == 201, creation.text

    reponse = await client.get(f"/visits/{voisine['visit'].id}", headers=voisine_headers)

    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["visitor"]["nom"] == "Ndoye"


SEC_005 = "SEC-005 : le verrouillage révèle l'existence du compte et se déclenche à distance"


async def _six_echecs(client: AsyncClient, identifiant: str) -> dict:
    reponse = None
    for _ in range(6):
        reponse = await client.post(
            "/auth/login", json={"identifiant": identifiant, "mot_de_passe": "mauvais"}
        )
    assert reponse is not None
    return reponse.json()


@pytest.mark.xfail(strict=True, reason=SEC_005)
async def test_sec005_compte_verrouille_et_compte_inconnu_repondent_pareil(
    client: AsyncClient, seeded: dict
) -> None:
    """Sans connaître aucun mot de passe, on ne doit pas distinguer un compte réel."""
    existant = await _six_echecs(client, "agent001")
    inconnu = await _six_echecs(client, "personne-ici")

    assert existant["error_code"] == inconnu["error_code"]


SEC_004 = "SEC-004 : le jeton d'accès survit à la déconnexion et à la révocation de session"


@pytest.mark.xfail(strict=True, reason=SEC_004)
async def test_sec004_jeton_dacces_refuse_apres_deconnexion(
    client: AsyncClient, seeded: dict
) -> None:
    """Une tablette déconnectée (ou révoquée) ne doit plus rien lire."""
    connexion = await client.post(
        "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "MotDePasse123!"}
    )
    jetons = connexion.json()
    entetes = {"Authorization": f"Bearer {jetons['access_token']}"}

    deconnexion = await client.post(
        "/auth/logout", json={"refresh_token": jetons["refresh_token"]}
    )
    assert deconnexion.status_code == 200, deconnexion.text

    reponse = await client.get("/visits", headers=entetes)
    assert reponse.status_code == 401
