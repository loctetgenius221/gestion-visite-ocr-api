"""Pipeline OCR MRZ complet (spec §4) : image → champs d'identité structurés.

Orchestration : preprocessing OpenCV → OCR PaddleOCR → parsing/validation ICAO 9303.
Les trois étapes vivent dans des modules distincts pour rester testables séparément
(`image_preprocessing`, `ocr_engine`, `mrz_parser`).
"""

from __future__ import annotations

import time

import anyio

from app.core.errors import MrzNotDetectedError, MrzParsingError
from app.core.logging import get_logger
from app.schemas.ocr import MrzScanResponse
from app.services.image_preprocessing import preprocess_candidates
from app.services.mrz_parser import parse_mrz_lines
from app.services.ocr_engine import OcrEngine, get_ocr_engine
from app.services.storage_service import StorageService, validate_image_upload

logger = get_logger(__name__)


class MrzOcrService:
    """Service applicatif du scan MRZ."""

    def __init__(
        self, engine: OcrEngine | None = None, storage: StorageService | None = None
    ) -> None:
        self._engine = engine or get_ocr_engine()
        self._storage = storage

    async def scan(
        self, content: bytes, filename: str | None, *, persist_image: bool = True
    ) -> MrzScanResponse:
        """Traite une image de document et retourne les champs MRZ extraits.

        Lève `MrzNotDetectedError` (422) si aucune bande MRZ exploitable n'est trouvée.
        Un échec de checksum ne lève pas : la réponse porte `mrz_valid=false`.
        """
        extension = validate_image_upload(filename, content)
        started = time.perf_counter()

        # Preprocessing + OCR sont bloquants (CPU) : on les sort de la boucle asyncio
        # pour ne pas geler l'event loop pendant le traitement.
        response = await anyio.to_thread.run_sync(self._run_blocking_pipeline, content)

        if response is None:
            # On ne logue jamais l'image ni son contenu (RGPD).
            logger.warning("Aucun MRZ exploitable sur l'image", extra={"stage": "pipeline"})
            raise MrzNotDetectedError()

        if persist_image and self._storage is not None:
            response.mrz_image_url = await self._storage.save(
                content, folder="mrz", extension=extension
            )

        response.processing_time_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "Scan MRZ traité",
            extra={
                "mrz_format": response.mrz_format.value,
                "mrz_valid": response.mrz_valid,
                "processing_time_ms": response.processing_time_ms,
                # Booléen et non la valeur : le NIN est une donnée personnelle, il
                # n'a rien à faire dans un journal. Savoir qu'il n'a pas été lu
                # suffit à repérer une régression de l'extraction en production.
                "nin_trouve": response.fields.nin is not None,
            },
        )
        return response

    def _run_blocking_pipeline(self, content: bytes) -> MrzScanResponse | None:
        """Tente l'OCR sur chaque orientation candidate, s'arrête à la première réussite.

        Une photo prise au téléphone peut être pivotée d'un quart de tour ; c'est le
        parsing qui tranche, un MRZ dont les checksums se recomposent ne pouvant pas
        être le fruit du hasard.
        """
        for candidate in preprocess_candidates(content):
            lines = self._engine.read_lines(candidate.image)
            if not lines:
                continue
            try:
                response = parse_mrz_lines(lines)
            except (MrzNotDetectedError, MrzParsingError):
                logger.info(
                    "Orientation écartée",
                    extra={"rotation": candidate.rotation, "candidate_lines": len(lines)},
                )
                continue

            if candidate.rotation:
                logger.info("MRZ lu après rotation", extra={"rotation": candidate.rotation})
            return response

        return None
