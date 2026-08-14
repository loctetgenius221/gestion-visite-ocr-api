"""Tests du dépôt de la signature manuscrite et des photos de pièce d'identité.

L'app mobile capture la signature sur un pad tactile puis la dépose ici : sans cet
endpoint, le champ `signature_url` de `POST /visits` serait inutilisable côté client.
Même logique pour les deux faces de la pièce en saisie manuelle (ADR-018).
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


class TestUploadDocument:
    """Photos recto et verso de la pièce, pour la saisie manuelle (ADR-018)."""

    @staticmethod
    async def _deposer(client: AsyncClient, auth_headers: dict, face: str):
        return await client.post(
            "/uploads/document",
            params={"face": face},
            files={"document": ("piece.png", signature_png(), "image/png")},
            headers=auth_headers,
        )

    async def test_les_deux_faces_sont_rangees_separement(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        recto = await self._deposer(client, auth_headers, "recto")
        verso = await self._deposer(client, auth_headers, "verso")

        assert recto.status_code == 201, recto.text
        assert recto.json()["url"].startswith("/storage/uploads/documents/recto/")
        assert verso.json()["url"].startswith("/storage/uploads/documents/verso/")

    async def test_face_inconnue_est_refusee(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await self._deposer(client, auth_headers, "dessus")

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_format_non_supporte_renvoie_400(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        response = await client.post(
            "/uploads/document",
            params={"face": "recto"},
            files={"document": ("piece.svg", b"<svg/>", "image/svg+xml")},
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert response.json()["error_code"] == "UNSUPPORTED_IMAGE"

    async def test_sans_authentification_renvoie_401(self, client: AsyncClient, seeded: dict):
        response = await client.post(
            "/uploads/document",
            params={"face": "recto"},
            files={"document": ("piece.png", signature_png(), "image/png")},
        )

        assert response.status_code == 401

    async def test_parcours_complet_saisie_manuelle(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Le geste visé : l'OCR a échoué, l'agent photographie les deux faces."""
        recto = (await self._deposer(client, auth_headers, "recto")).json()["url"]
        verso = (await self._deposer(client, auth_headers, "verso")).json()["url"]

        payload = visit_payload(seeded)
        payload["visitor"]["document_recto_url"] = recto
        payload["visitor"]["document_verso_url"] = verso
        visite = await client.post("/visits", json=payload, headers=auth_headers)

        assert visite.status_code == 201, visite.text
        visiteur = visite.json()["visitor"]
        assert visiteur["document_recto_url"] == recto
        assert visiteur["document_verso_url"] == verso


class TestRetroCompatibiliteMrzImageUrl:
    """`mrz_image_url` désigne la même face que `document_verso_url` (ADR-018)."""

    async def test_lancien_champ_alimente_le_nouveau(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        """Une tablette pas encore mise à jour ne doit rien perdre."""
        payload = visit_payload(seeded)
        payload["visitor"]["mrz_image_url"] = "/storage/uploads/mrz/2026/08/abc.png"

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.status_code == 201
        visiteur = response.json()["visitor"]
        assert visiteur["document_verso_url"] == "/storage/uploads/mrz/2026/08/abc.png"
        assert visiteur["mrz_image_url"] == "/storage/uploads/mrz/2026/08/abc.png"

    async def test_le_nouveau_champ_prime_si_les_deux_sont_fournis(
        self, client: AsyncClient, seeded: dict, auth_headers: dict
    ):
        payload = visit_payload(seeded)
        payload["visitor"]["mrz_image_url"] = "/storage/uploads/mrz/2026/08/ancien.png"
        payload["visitor"]["document_verso_url"] = "/storage/uploads/documents/verso/neuf.png"

        response = await client.post("/visits", json=payload, headers=auth_headers)

        assert response.json()["visitor"]["document_verso_url"].endswith("neuf.png")
