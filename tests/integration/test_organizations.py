from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole


@pytest.mark.asyncio
async def test_create_organization_with_admin(
    client: AsyncClient, admin_headers: dict[str, str], session: AsyncSession
) -> None:
    """Création d'une organisation avec son premier administrateur."""
    payload = {
        "nom": "Ministère de la Santé",
        "code": "MSAS",
        "localisation": "Fann Résidence, Dakar",
        "description": "Ministère de la Santé et de l'Action Sociale",
        "contact_email": "contact@sante.gouv.sn",
        "contact_phone": "+221 33 869 42 42",
        "admin_nom": "Dr. Fatou Diop",
        "admin_identifiant": "admin.msas",
    }
    response = await client.post("/organizations", json=payload, headers=admin_headers)
    assert response.status_code == 201, response.text
    data = response.json()

    # Vérification de l'organisation
    org = data["organization"]
    assert org["nom"] == "Ministère de la Santé"
    assert org["code"] == "MSAS"
    assert org["localisation"] == "Fann Résidence, Dakar"
    assert org["is_active"] is True

    # Vérification de l'admin créé
    admin_user = data["admin_user"]
    assert admin_user is not None
    assert admin_user["identifiant"] == "admin.msas"
    assert admin_user["role"] == UserRole.ORG_ADMIN.value
    assert data["admin_temporary_password"] is not None

    # Test de connexion avec le nouvel admin créé
    login_resp = await client.post(
        "/auth/login",
        json={"identifiant": "admin.msas", "mot_de_passe": data["admin_temporary_password"]},
    )
    assert login_resp.status_code == 200


@pytest.mark.asyncio
async def test_create_organization_duplicate_code(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Le code d'une organisation doit être unique."""
    payload = {
        "nom": "Port Autonome de Dakar",
        "code": "PAD",
        "localisation": "Dakar Port",
    }
    resp1 = await client.post("/organizations", json=payload, headers=admin_headers)
    assert resp1.status_code == 201

    # Tentative avec le même code (casse insensible)
    payload_dup = {
        "nom": "PAD Bis",
        "code": "pad",
    }
    resp2 = await client.post("/organizations", json=payload_dup, headers=admin_headers)
    assert resp2.status_code == 409
    assert resp2.json()["error_code"] == "DUPLICATE_ORGANIZATION_CODE"


@pytest.mark.asyncio
async def test_list_and_search_organizations(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Listing paginé et recherche d'organisations."""
    await client.post(
        "/organizations",
        json={"nom": "Douanes Sénégalaises", "code": "DGD", "localisation": "Bloc des Madeleines"},
        headers=admin_headers,
    )
    await client.post(
        "/organizations",
        json={
            "nom": "Direction Générale des Impôts",
            "code": "DGID",
            "localisation": "Dakar Plateau",
        },
        headers=admin_headers,
    )

    # Liste globale
    res = await client.get("/organizations", headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 2

    # Recherche par nom
    res_search = await client.get("/organizations?search=Douanes", headers=admin_headers)
    assert res_search.status_code == 200
    search_data = res_search.json()
    assert search_data["total"] == 1
    assert search_data["items"][0]["code"] == "DGD"


@pytest.mark.asyncio
async def test_get_and_update_organization(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Consultation et modification des détails d'une structure."""
    create_resp = await client.post(
        "/organizations",
        json={"nom": "APIX", "code": "APIX", "localisation": "Dakar"},
        headers=admin_headers,
    )
    org_id = create_resp.json()["organization"]["id"]

    # Consultation
    get_resp = await client.get(f"/organizations/{org_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["nom"] == "APIX"

    # Mise à jour
    update_resp = await client.put(
        f"/organizations/{org_id}",
        json={
            "localisation": "52, Rue Wagane Diouf, Dakar",
            "description": "Promotion des Investissements",
        },
        headers=admin_headers,
    )
    assert update_resp.status_code == 200
    updated_data = update_resp.json()
    assert updated_data["localisation"] == "52, Rue Wagane Diouf, Dakar"
    assert updated_data["description"] == "Promotion des Investissements"


@pytest.mark.asyncio
async def test_organization_stats(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Consultation des statistiques de volume d'une organisation."""
    create_resp = await client.post(
        "/organizations",
        json={"nom": "Senelec", "code": "SENELEC", "localisation": "Dakar"},
        headers=admin_headers,
    )
    org_id = create_resp.json()["organization"]["id"]

    stats_resp = await client.get(f"/organizations/{org_id}/stats", headers=admin_headers)
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["organization_id"] == org_id
    assert stats["total_services"] == 0
    assert stats["total_visits"] == 0


@pytest.mark.asyncio
async def test_organization_forbidden_for_agent(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Un simple agent de contrôle ne peut pas lister ni créer d'organisation."""
    res = await client.get("/organizations", headers=auth_headers)
    assert res.status_code == 403

    post_res = await client.post(
        "/organizations",
        json={"nom": "Fraude", "code": "FRAUDE"},
        headers=auth_headers,
    )
    assert post_res.status_code == 403
