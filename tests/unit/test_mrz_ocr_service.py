"""Tests de l'orchestration du pipeline OCR MRZ.

Le moteur PaddleOCR est doublé par un simulateur qui refuse le texte pivoté, ce qui
permet de vérifier que le service récupère bien une photo prise de travers — cas
observé sur une photo réelle de CNI tenue en portrait.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.services.image_preprocessing import rotate
from app.services.mrz_ocr_service import MrzOcrService
from tests.unit.test_mrz_parser import CNI_SEN_LINES, CNI_SEN_NUMERO


def carte_photographiee(angle: int) -> bytes:
    """Verso de CNI synthétique, photographié avec une rotation de `angle` degrés."""
    image = np.full((620, 980, 3), 243, dtype=np.uint8)
    cv2.putText(
        image, "INFORMATIONS ELECTORALES", (60, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 120, 60), 2
    )
    for index, ligne in enumerate(CNI_SEN_LINES):
        cv2.putText(
            image, ligne, (40, 430 + index * 60), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (15, 15, 15), 2
        )
    ok, buffer = cv2.imencode(".jpg", rotate(image, angle))
    assert ok
    return buffer.tobytes()


class OcrSensibleALOrientation:
    """Double de PaddleOCR qui ne lit que du texte horizontal.

    Décide via le profil de projection : du texte en lignes crée une forte alternance
    de densité d'encre entre lignes pleines et interlignes, absente s'il est pivoté.
    """

    def __init__(self, lignes: list[str] | None = None) -> None:
        self.lignes = lignes if lignes is not None else list(CNI_SEN_LINES)
        self.passes = 0

    def read_lines(self, image: np.ndarray) -> list[str]:
        self.passes += 1
        encre = (image < 128).astype(np.float32)
        if encre.sum() < 50:
            return []
        horizontal = encre.mean(axis=1).std() > encre.mean(axis=0).std() * 1.5
        return list(self.lignes) if horizontal else ["TEXTE ILLISIBLE CAR PIVOTE"]


class TestRecuperationDeLOrientation:
    @pytest.mark.parametrize("angle", [0, 90, 180, 270])
    def test_le_mrz_est_lu_quelle_que_soit_lorientation(self, angle: int):
        engine = OcrSensibleALOrientation()
        service = MrzOcrService(engine=engine, storage=None)

        response = service._run_blocking_pipeline(carte_photographiee(angle))

        assert response is not None, f"MRZ non récupéré sur une photo pivotée de {angle}°"
        assert response.mrz_valid is True
        assert response.fields.nom == "NDIAYE"
        assert response.fields.numero_document == CNI_SEN_NUMERO

    def test_une_photo_droite_ne_coute_quune_passe_ocr(self):
        """Le multi-orientation ne doit rien coûter dans le cas nominal (budget 3 s)."""
        engine = OcrSensibleALOrientation()

        MrzOcrService(engine=engine, storage=None)._run_blocking_pipeline(carte_photographiee(0))

        assert engine.passes == 1

    def test_une_photo_pivotee_reste_sous_quatre_passes(self):
        engine = OcrSensibleALOrientation()

        MrzOcrService(engine=engine, storage=None)._run_blocking_pipeline(carte_photographiee(90))

        assert 1 < engine.passes <= 4


class TestEchecs:
    def test_texte_sans_mrz_ne_produit_aucun_resultat(self):
        engine = OcrSensibleALOrientation(lignes=["REPUBLIQUE DU SENEGAL", "CARTE D IDENTITE"])
        service = MrzOcrService(engine=engine, storage=None)

        assert service._run_blocking_pipeline(carte_photographiee(0)) is None

    def test_ocr_muet_ne_produit_aucun_resultat(self):
        engine = OcrSensibleALOrientation(lignes=[])
        service = MrzOcrService(engine=engine, storage=None)

        assert service._run_blocking_pipeline(carte_photographiee(0)) is None

    def test_toutes_les_orientations_sont_tentees_avant_dabandonner(self):
        engine = OcrSensibleALOrientation(lignes=["AUCUN MRZ ICI"])

        MrzOcrService(engine=engine, storage=None)._run_blocking_pipeline(carte_photographiee(0))

        assert engine.passes == 4
