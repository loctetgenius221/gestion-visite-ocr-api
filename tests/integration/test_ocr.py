"""Tests de `POST /ocr/scan`.

Le moteur PaddleOCR est remplacé par un double qui renvoie des lignes MRZ fixes :
on valide ici le contrat HTTP et l'intégration preprocessing → parsing, sans
dépendre du modèle OCR ni d'une photo réelle de document.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import cv2
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_mrz_ocr_service
from app.main import create_app
from app.services.mrz_ocr_service import MrzOcrService
from tests.unit.test_mrz_parser import (
    CNI_SEN_LINES,
    CNI_SEN_NUMERO,
    TD1_LINES,
    TD3_LINES,
)


class FakeOcrEngine:
    """Double du moteur OCR : renvoie des lignes prédéfinies, ignore l'image."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.calls = 0

    def read_lines(self, image: np.ndarray) -> list[str]:
        self.calls += 1
        return list(self.lines)


def fake_document_image() -> bytes:
    """Image JPEG synthétique, suffisante pour traverser le preprocessing OpenCV."""
    canvas = np.full((600, 900, 3), 255, dtype=np.uint8)
    for index, line in enumerate(TD3_LINES):
        cv2.putText(
            canvas, line, (20, 480 + index * 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1
        )
    ok, buffer = cv2.imencode(".jpg", canvas)
    assert ok
    return buffer.tobytes()


@pytest.fixture
def ocr_client_factory(engine, session: AsyncSession):
    """Fabrique un client HTTP dont le moteur OCR renvoie les lignes demandées."""

    def build(lines: list[str]) -> tuple[AsyncClient, FakeOcrEngine]:
        app = create_app()
        fake_engine = FakeOcrEngine(lines)

        async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
            yield session

        # `persist_image=False` côté service : on ne veut pas écrire sur disque en test.
        def override_service() -> MrzOcrService:
            return MrzOcrService(engine=fake_engine, storage=None)

        app.dependency_overrides[get_session] = override_get_session
        app.dependency_overrides[get_mrz_ocr_service] = override_service

        client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1"
        )
        return client, fake_engine

    return build


class TestScanMrz:
    async def test_scan_td3_retourne_le_schema_de_la_spec(
        self, ocr_client_factory, seeded: dict, auth_headers: dict
    ):
        client, fake_engine = ocr_client_factory(TD3_LINES)
        async with client:
            response = await client.post(
                "/ocr/scan",
                files={"mrz_image": ("passeport.jpg", fake_document_image(), "image/jpeg")},
                headers=auth_headers,
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["document_type"] == "PASSEPORT"
        assert body["mrz_format"] == "TD3"
        assert body["mrz_valid"] is True
        assert body["fields"]["nom"] == "DIOP"
        assert body["fields"]["prenom"] == "AMINATA"
        assert body["fields"]["date_naissance"] == "1990-05-14"
        assert body["checksum_details"] == {
            "document_number": True,
            "date_of_birth": True,
            "expiration_date": True,
            "composite": True,
        }
        assert body["raw_mrz_lines"] == TD3_LINES
        assert fake_engine.calls == 1

    async def test_scan_td1_reconnait_une_cni(
        self, ocr_client_factory, seeded: dict, auth_headers: dict
    ):
        client, _ = ocr_client_factory(TD1_LINES)
        async with client:
            response = await client.post(
                "/ocr/scan",
                files={"mrz_image": ("cni.png", fake_document_image(), "image/png")},
                headers=auth_headers,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["document_type"] == "CNI"
        assert body["mrz_format"] == "TD1"
        assert body["mrz_valid"] is True

    async def test_scan_cni_senegalaise_bout_en_bout(
        self, ocr_client_factory, seeded: dict, auth_headers: dict
    ):
        """Structure réelle d'une CNI CEDEAO : numéro de carte débordant du champ MRZ."""
        client, _ = ocr_client_factory(CNI_SEN_LINES)
        async with client:
            response = await client.post(
                "/ocr/scan",
                files={"mrz_image": ("cni.jpg", fake_document_image(), "image/jpeg")},
                headers=auth_headers,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["mrz_valid"] is True
        # Numéro complet reconstitué à partir du débordement, et non tronqué à 9.
        assert body["fields"]["numero_document"] == CNI_SEN_NUMERO
        assert body["checksum_details"]["document_number"] is True
        # Le NIN n'existe pas dans le MRZ : saisie manuelle côté agent (ADR-005).
        assert body["fields"]["nin"] is None

    async def test_aucun_mrz_lisible_renvoie_422_au_format_standard(
        self, ocr_client_factory, seeded: dict, auth_headers: dict
    ):
        client, _ = ocr_client_factory(["Ministère de la Fonction Publique"])
        async with client:
            response = await client.post(
                "/ocr/scan",
                files={"mrz_image": ("photo.jpg", fake_document_image(), "image/jpeg")},
                headers=auth_headers,
            )

        assert response.status_code == 422
        body = response.json()
        assert body["error_code"] == "MRZ_NOT_DETECTED"
        assert set(body) == {"error_code", "message", "details"}

    async def test_aucune_ligne_extraite_renvoie_422(
        self, ocr_client_factory, seeded: dict, auth_headers: dict
    ):
        client, _ = ocr_client_factory([])
        async with client:
            response = await client.post(
                "/ocr/scan",
                files={"mrz_image": ("photo.jpg", fake_document_image(), "image/jpeg")},
                headers=auth_headers,
            )

        assert response.status_code == 422
        assert response.json()["error_code"] == "MRZ_NOT_DETECTED"

    async def test_extension_non_supportee_renvoie_400(
        self, ocr_client_factory, seeded: dict, auth_headers: dict
    ):
        client, _ = ocr_client_factory(TD3_LINES)
        async with client:
            response = await client.post(
                "/ocr/scan",
                files={"mrz_image": ("document.pdf", b"%PDF-1.4", "application/pdf")},
                headers=auth_headers,
            )

        assert response.status_code == 400
        assert response.json()["error_code"] == "UNSUPPORTED_IMAGE"

    async def test_fichier_manquant_renvoie_400(
        self, ocr_client_factory, seeded: dict, auth_headers: dict
    ):
        client, _ = ocr_client_factory(TD3_LINES)
        async with client:
            response = await client.post("/ocr/scan", headers=auth_headers)

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_sans_authentification_renvoie_401(self, ocr_client_factory, seeded: dict):
        client, _ = ocr_client_factory(TD3_LINES)
        async with client:
            response = await client.post(
                "/ocr/scan",
                files={"mrz_image": ("photo.jpg", fake_document_image(), "image/jpeg")},
            )

        assert response.status_code == 401
