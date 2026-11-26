"""Preuves de l'audit du 26/11/2026 (voir audit/AUDIT_2026-11-26.md).

Chaque test décrit le comportement **attendu**. Tant que le constat n'est pas
corrigé, il est marqué `xfail(strict=True)` : la suite reste verte, et le jour où
le correctif arrive, le test passe, `strict` le signale, et il suffit de retirer
le marqueur pour en faire un test de non-régression.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

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


# SEC-005 : le verrouillage révélait l'existence du compte. Corrigé en vérifiant
# le mot de passe avant le verrou (non-régression).


async def _six_echecs(client: AsyncClient, identifiant: str) -> dict:
    reponse = None
    for _ in range(6):
        reponse = await client.post(
            "/auth/login", json={"identifiant": identifiant, "mot_de_passe": "mauvais"}
        )
    assert reponse is not None
    return reponse.json()


async def test_sec005_compte_verrouille_et_compte_inconnu_repondent_pareil(
    client: AsyncClient, seeded: dict
) -> None:
    """Sans connaître aucun mot de passe, on ne doit pas distinguer un compte réel."""
    existant = await _six_echecs(client, "agent001")
    inconnu = await _six_echecs(client, "personne-ici")

    assert existant["error_code"] == inconnu["error_code"]


async def test_sec005_le_titulaire_apprend_que_son_compte_est_verrouille(
    client: AsyncClient, seeded: dict
) -> None:
    await _six_echecs(client, "agent001")

    reponse = await client.post(
        "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "MotDePasse123!"}
    )

    assert reponse.json()["error_code"] == "LOCKED_ACCOUNT"


async def test_sec006_lincrement_des_echecs_ne_repart_pas_dune_valeur_perimee(
    session: AsyncSession, seeded: dict
) -> None:
    """Deux requêtes simultanées lisent la même valeur : chacune doit compter."""
    from app.repositories.user_repository import UserRepository

    depot = UserRepository(session)
    utilisateur = await depot.get_by_identifiant("agent001")
    assert utilisateur is not None
    depart = utilisateur.failed_login_attempts

    assert await depot.incrementer_echecs(utilisateur) == depart + 1
    # Une requête concurrente a lu le compteur avant ce premier incrément : son
    # instance porte encore l'ancienne valeur. Un `+= 1` en Python écrirait
    # `depart + 1` une seconde fois.
    set_committed_value(utilisateur, "failed_login_attempts", depart)

    assert await depot.incrementer_echecs(utilisateur) == depart + 2


# SEC-004 : le jeton d'accès survivait à la déconnexion et à la révocation de
# session. Corrigé par la claim `sid` (non-régression).


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


async def test_sec004_jeton_dacces_refuse_apres_revocation_par_un_admin(
    client: AsyncClient, seeded: dict, admin_headers: dict[str, str]
) -> None:
    """La tablette perdue : l'admin coupe la session, le jeton en cours tombe."""
    connexion = await client.post(
        "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "MotDePasse123!"}
    )
    entetes = {"Authorization": f"Bearer {connexion.json()['access_token']}"}
    utilisateur = connexion.json()["user"]["id"]
    sessions = await client.get(f"/users/{utilisateur}/sessions", headers=admin_headers)
    assert sessions.status_code == 200, sessions.text
    for session in sessions.json():
        coupure = await client.delete(
            f"/users/{utilisateur}/sessions/{session['id']}", headers=admin_headers
        )
        assert coupure.status_code in (200, 204), coupure.text

    assert (await client.get("/visits", headers=entetes)).status_code == 401


async def test_sec004_le_jeton_dacces_reste_valide_apres_un_rafraichissement(
    client: AsyncClient, seeded: dict
) -> None:
    connexion = (
        await client.post(
            "/auth/login", json={"identifiant": "agent001", "mot_de_passe": "MotDePasse123!"}
        )
    ).json()
    rafraichi = await client.post(
        "/auth/refresh", json={"refresh_token": connexion["refresh_token"]}
    )
    assert rafraichi.status_code == 200, rafraichi.text
    for jeton in (connexion["access_token"], rafraichi.json()["access_token"]):
        reponse = await client.get("/visits", headers={"Authorization": f"Bearer {jeton}"})
        assert reponse.status_code == 200, reponse.text


async def test_sec009_une_visite_ne_sinscrit_pas_lan_dernier(
    client: AsyncClient, seeded: dict, auth_headers: dict[str, str]
) -> None:
    from datetime import UTC, datetime, timedelta

    payload = _visite_par_saisie(str(seeded["service"].id), "OLD0000001")
    payload["checked_in_at"] = (datetime.now(UTC) - timedelta(days=365)).isoformat()

    reponse = await client.post("/visits", json=payload, headers=auth_headers)

    assert reponse.status_code == 422, reponse.text
    assert reponse.json()["error_code"] == "TIMESTAMP_TOO_OLD"


async def test_sec009_une_tablette_hors_ligne_quelques_jours_se_synchronise(
    client: AsyncClient, seeded: dict, auth_headers: dict[str, str]
) -> None:
    from datetime import UTC, datetime, timedelta

    payload = _visite_par_saisie(str(seeded["service"].id), "OLD0000002")
    payload["checked_in_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()

    reponse = await client.post("/visits", json=payload, headers=auth_headers)

    assert reponse.status_code == 201, reponse.text


async def test_sec013_les_jokers_saisis_ne_listent_pas_tout(
    client: AsyncClient, seeded: dict, admin_headers: dict[str, str]
) -> None:
    """`%%%` et `___` sont cherchés tels quels, pas comme des jokers SQL."""
    tous = await client.get("/users", headers=admin_headers)
    assert tous.json()["total"] > 0

    for terme in ("%%%", "___"):
        reponse = await client.get("/users", params={"search": terme}, headers=admin_headers)
        assert reponse.status_code == 200, reponse.text
        assert reponse.json()["total"] == 0


def test_sec013_motif_echappe() -> None:
    from app.core.recherche import motif_contient

    assert motif_contient(r"a%b_c\d") == r"%a\%b\_c\\d%"


async def test_sec011_un_org_admin_ne_reinitialise_pas_un_pair(
    client: AsyncClient,
    session: AsyncSession,
    seeded: dict,
    org_admin_headers: dict[str, str],
) -> None:
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.user import User

    pair = User(
        nom="Second administrateur",
        identifiant="orgadmin002",
        mot_de_passe_hash=hash_password("MotDePasse123!"),
        role=UserRole.ORG_ADMIN,
        organization_id=seeded["organization"].id,
        is_active=True,
    )
    session.add(pair)
    await session.commit()

    reponse = await client.post(
        f"/users/{pair.id}/reset-password", headers=org_admin_headers, json={}
    )

    assert reponse.status_code == 403, reponse.text
    assert "mot_de_passe" not in reponse.json()


async def test_sec011_ladministrateur_de_plateforme_le_peut(
    client: AsyncClient, org_admin, admin_headers: dict[str, str]
) -> None:
    reponse = await client.post(
        f"/users/{org_admin.id}/reset-password", headers=admin_headers, json={}
    )

    assert reponse.status_code == 200, reponse.text


async def test_sec011_un_org_admin_reinitialise_toujours_un_agent(
    client: AsyncClient, seeded: dict, org_admin_headers: dict[str, str]
) -> None:
    reponse = await client.post(
        f"/users/{seeded['user'].id}/reset-password", headers=org_admin_headers, json={}
    )

    assert reponse.status_code == 200, reponse.text
