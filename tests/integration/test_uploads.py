"""Tests du dépôt de la signature manuscrite.

L'app mobile capture la signature sur un pad tactile puis la dépose ici : sans cet
endpoint, le champ `signature_url` de `POST /visits` serait inutilisable côté client.
"""

from __future__ import annotations

import cv2
import numpy as np
from httpx import AsyncClient

from tests.integration.test_visits import visit_payload


def signature_png() -> bytes:
    """Trait manuscrit synthétique sur fond blanc, comme un pad de signature."""
    canvas = np.full((200, 600, 3), 255, dtype=np.uint8)
    points = np.array([[50, 150], [150, 60], [250, 160], [350, 70], [450, 150]], dtype=np.int32)
    cv2.polylines(canvas, [points], isClosed=False, color=(0, 0, 0), thickness=4)
    ok, buffer = cv2.imencode(".png", canvas)
    assert ok
    return buffer.tobytes()


class TestUploadSignature:
    async def test_depot_retourne_une_url_exploitable(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.post(
            "/uploads/signature",
            files={"signature": ("signature.png", signature_png(), "image/png")},
            headers=auth_headers,
        )

        assert response.status_code == 201, response.text
        url = response.json()["url"]
        assert url.startswith("/storage/uploads/signatures/")
        assert url.endswith(".png")

    async def test_deux_depots_ne_se_recouvrent_pas(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        premier = await client.post(
            "/uploads/signature",
            files={"signature": ("s.png", signature_png(), "image/png")},
            headers=auth_headers,
        )
        second = await client.post(
            "/uploads/signature",
            files={"signature": ("s.png", signature_png(), "image/png")},
            headers=auth_headers,
        )

        assert premier.json()["url"] != second.json()["url"]

    async def test_lurl_deposee_est_conservee_sur_la_visite(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Parcours complet côté mobile : dépôt de la signature puis création de visite."""
        depot = await client.post(
            "/uploads/signature",
            files={"signature": ("signature.png", signature_png(), "image/png")},
            headers=auth_headers,
        )
        url = depot.json()["url"]

        payload = visit_payload(seeded, signature_url=url)
        visite = await client.post("/visits", json=payload, headers=auth_headers)

        assert visite.status_code == 201
        assert visite.json()["signature_url"] == url

    async def test_format_non_supporte_renvoie_400(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.post(
            "/uploads/signature",
            files={"signature": ("signature.svg", b"<svg/>", "image/svg+xml")},
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "UNSUPPORTED_IMAGE"

    async def test_fichier_vide_renvoie_400(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.post(
            "/uploads/signature",
            files={"signature": ("signature.png", b"", "image/png")},
            headers=auth_headers,
        )

        assert response.status_code == 400

    async def test_sans_authentification_renvoie_401(self, client: AsyncClient, seeded: dict):
        response = await client.post(
            "/uploads/signature",
            files={"signature": ("signature.png", signature_png(), "image/png")},
        )

        assert response.status_code == 401
