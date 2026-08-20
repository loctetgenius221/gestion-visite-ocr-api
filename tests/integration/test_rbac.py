"""Le contrôle de rôle est la garantie centrale du dashboard : il est testé route par route.

Un oubli de garde sur une seule route ouvrirait à tout agent de contrôle la
gestion des comptes ou le journal d'audit. Ces tests énumèrent donc l'ensemble
des routes d'administration plutôt que d'en échantillonner quelques-unes.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

# (méthode, chemin, corps) — chemin relatif au préfixe /api/v1.
ROUTES_ADMIN: list[tuple[str, str, dict | None]] = [
    ("GET", "/users", None),
    ("POST", "/users", {"nom": "X", "identifiant": "xxx001", "role": "AGENT_CONTROLE"}),
    ("GET", "/users/00000000-0000-0000-0000-000000000001", None),
    ("PUT", "/users/00000000-0000-0000-0000-000000000001", {"nom": "X"}),
    ("PATCH", "/users/00000000-0000-0000-0000-000000000001/status", {"status": "inactive"}),
    ("POST", "/users/00000000-0000-0000-0000-000000000001/reset-password", {}),
    ("POST", "/users/00000000-0000-0000-0000-000000000001/unlock", None),
    ("GET", "/users/00000000-0000-0000-0000-000000000001/sessions", None),
    (
        "DELETE",
        "/users/00000000-0000-0000-0000-000000000001/sessions/"
        "00000000-0000-0000-0000-000000000002",
        None,
    ),
    ("GET", "/audit-logs", None),
    ("GET", "/audit-logs/actions", None),
    ("GET", "/settings", None),
    ("PUT", "/settings", {"max_failed_login_attempts": 5}),
    ("POST", "/services", {"name": "X", "code": "XXX"}),
    ("PUT", "/services/00000000-0000-0000-0000-000000000001", {"name": "X"}),
    ("PATCH", "/services/00000000-0000-0000-0000-000000000001/status", {"status": "archived"}),
    (
        "POST",
        "/agents",
        {"name": "X", "service_id": "00000000-0000-0000-0000-000000000001"},
    ),
    ("PUT", "/agents/00000000-0000-0000-0000-000000000001", {"name": "X"}),
    ("PATCH", "/agents/00000000-0000-0000-0000-000000000001/status", {"status": "archived"}),
    ("POST", "/purposes", {"libelle": "X"}),
    ("PUT", "/purposes/00000000-0000-0000-0000-000000000001", {"libelle": "X"}),
    ("PATCH", "/purposes/00000000-0000-0000-0000-000000000001/status", {"status": "archived"}),
    ("PATCH", "/visits/00000000-0000-0000-0000-000000000001", {"reason": "erreur", "nom": None}),
    ("POST", "/visits/00000000-0000-0000-0000-000000000001/cancel", {"reason": "doublon"}),
    ("DELETE", "/visits/00000000-0000-0000-0000-000000000001", None),
    ("GET", "/visits/export?format=csv", None),
    ("GET", "/dashboard/stats/timeseries", None),
    ("GET", "/dashboard/stats/by-service", None),
    ("GET", "/dashboard/stats/by-purpose", None),
    ("GET", "/dashboard/stats/peak-hours", None),
    ("GET", "/dashboard/stats/avg-duration", None),
    ("GET", "/dashboard/stats/top-agents", None),
]

IDS = [f"{methode} {chemin}" for methode, chemin, _ in ROUTES_ADMIN]


@pytest.mark.parametrize(("methode", "chemin", "corps"), ROUTES_ADMIN, ids=IDS)
async def test_un_agent_de_controle_est_refuse(
    client: AsyncClient,
    auth_headers: dict[str, str],
    methode: str,
    chemin: str,
    corps: dict | None,
) -> None:
    response = await client.request(methode, chemin, headers=auth_headers, json=corps)

    assert response.status_code == 403, f"{methode} {chemin} a répondu {response.status_code}"
    assert response.json()["error_code"] == "FORBIDDEN"


@pytest.mark.parametrize(("methode", "chemin", "corps"), ROUTES_ADMIN, ids=IDS)
async def test_sans_token_la_reponse_est_401(
    client: AsyncClient, methode: str, chemin: str, corps: dict | None
) -> None:
    response = await client.request(methode, chemin, json=corps)

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"


@pytest.mark.parametrize(("methode", "chemin", "corps"), ROUTES_ADMIN, ids=IDS)
async def test_un_admin_franchit_le_garde(
    client: AsyncClient,
    admin_headers: dict[str, str],
    methode: str,
    chemin: str,
    corps: dict | None,
) -> None:
    """L'admin ne doit jamais recevoir 401/403 — le reste (404, 400) est hors sujet ici."""
    response = await client.request(methode, chemin, headers=admin_headers, json=corps)

    assert response.status_code not in (401, 403), (
        f"{methode} {chemin} a répondu {response.status_code} : {response.text}"
    )


class TestRoleLuEnBase:
    async def test_le_role_du_jwt_nest_pas_cru_sur_parole(
        self, client: AsyncClient, admin_headers: dict[str, str], session, admin
    ) -> None:
        """Rétrograder un compte doit fermer le dashboard immédiatement.

        Le jeton déjà émis porte encore `role: ADMIN` dans ses revendications : si
        le garde s'y fiait, l'accès resterait ouvert jusqu'à son expiration.
        """
        from app.models.enums import UserRole

        assert (await client.get("/users", headers=admin_headers)).status_code == 200

        admin.role = UserRole.AGENT_CONTROLE
        await session.commit()

        response = await client.get("/users", headers=admin_headers)
        assert response.status_code == 403
        assert response.json()["error_code"] == "FORBIDDEN"

    async def test_un_superviseur_na_aucun_droit_admin(
        self, client: AsyncClient, session, seeded
    ) -> None:
        """SUPERVISEUR est conservé dans l'enum mais ne porte aucun privilège."""
        from app.core.security import hash_password
        from app.models.enums import UserRole
        from app.models.user import User
        from tests.conftest import TEST_PASSWORD

        session.add(
            User(
                nom="Superviseur",
                identifiant="superviseur001",
                mot_de_passe_hash=hash_password(TEST_PASSWORD),
                role=UserRole.SUPERVISEUR,
                is_active=True,
            )
        )
        await session.commit()

        login = await client.post(
            "/auth/login",
            json={"identifiant": "superviseur001", "mot_de_passe": TEST_PASSWORD},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        assert (await client.get("/users", headers=headers)).status_code == 403
        # ...mais il garde l'accès en lecture des référentiels, comme un agent.
        assert (await client.get("/services", headers=headers)).status_code == 200
