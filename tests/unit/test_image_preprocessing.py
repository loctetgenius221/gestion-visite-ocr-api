"""Tests du preprocessing OpenCV, sur des images synthétiques."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.core.errors import UnsupportedImageError
from app.services.image_preprocessing import (
    binarize,
    decode_image,
    deskew,
    detect_mrz_region,
    enhance_contrast,
    estimate_skew_angle,
    preprocess_candidates,
    preprocess_for_ocr,
    resize_to_working_width,
    rotate,
    to_grayscale,
)


def document_avec_mrz(width: int = 900, height: int = 600) -> np.ndarray:
    """Document synthétique : zone claire en haut, bande MRZ dense en bas."""
    canvas = np.full((height, width, 3), 245, dtype=np.uint8)
    cv2.putText(
        canvas, "REPUBLIQUE DU SENEGAL", (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (60, 60, 60), 2
    )
    for index in range(2):
        cv2.putText(
            canvas,
            "P<SENDIOP<<AMINATA<<<<<<<<<<<<<<<<<<<<<<<<<<",
            (20, 470 + index * 45),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            1,
        )
    return canvas


def encode(image: np.ndarray, extension: str = ".jpg") -> bytes:
    ok, buffer = cv2.imencode(extension, image)
    assert ok
    return buffer.tobytes()


class TestDecode:
    def test_decode_un_jpeg(self):
        image = decode_image(encode(document_avec_mrz()))
        assert image.shape[2] == 3

    def test_decode_un_png(self):
        image = decode_image(encode(document_avec_mrz(), ".png"))
        assert image.ndim == 3

    def test_contenu_illisible_leve_une_erreur_explicite(self):
        with pytest.raises(UnsupportedImageError):
            decode_image(b"ceci n'est pas une image")


class TestTransformations:
    def test_redimensionnement_borne_la_largeur(self):
        image = np.zeros((1200, 4000, 3), dtype=np.uint8)
        assert resize_to_working_width(image).shape[1] == 1600

    def test_image_plus_petite_que_la_cible_est_inchangee(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        assert resize_to_working_width(image).shape == image.shape

    def test_niveaux_de_gris_et_idempotence(self):
        gray = to_grayscale(document_avec_mrz())
        assert gray.ndim == 2
        assert to_grayscale(gray) is gray

    def test_clahe_conserve_les_dimensions(self):
        gray = to_grayscale(document_avec_mrz())
        assert enhance_contrast(gray).shape == gray.shape

    def test_binarisation_ne_produit_que_du_noir_et_du_blanc(self):
        binaire = binarize(to_grayscale(document_avec_mrz()))
        assert set(np.unique(binaire)).issubset({0, 255})

    def test_image_droite_nest_pas_pivotee(self):
        image = document_avec_mrz()
        assert deskew(image).shape == image.shape

    def test_angle_estime_sur_une_image_uniforme_est_nul(self):
        assert estimate_skew_angle(np.full((200, 200), 255, dtype=np.uint8)) == 0.0


class TestDetectionMrz:
    def test_la_bande_mrz_est_localisee_dans_la_moitie_basse(self):
        gray = to_grayscale(document_avec_mrz())
        region = detect_mrz_region(gray)

        assert region is not None
        x, y, w, h = region
        assert w / gray.shape[1] >= 0.6
        assert y + h > gray.shape[0] * 0.5

    def test_image_uniforme_ne_donne_aucune_region(self):
        assert detect_mrz_region(np.full((400, 600), 200, dtype=np.uint8)) is None


class TestOrientation:
    """Une photo prise au téléphone peut arriver pivotée d'un quart de tour."""

    def test_rotation_sans_angle_retourne_limage_telle_quelle(self):
        image = document_avec_mrz()
        assert rotate(image, 0) is image

    @pytest.mark.parametrize("angle", [90, 270])
    def test_les_quarts_de_tour_transposent_les_dimensions(self, angle: int):
        image = document_avec_mrz(width=900, height=600)
        pivotee = rotate(image, angle)
        assert pivotee.shape[:2] == (900, 600)

    def test_180_degres_conserve_les_dimensions(self):
        image = document_avec_mrz(width=900, height=600)
        assert rotate(image, 180).shape[:2] == (600, 900)

    def test_quatre_quarts_de_tour_reviennent_a_lorigine(self):
        image = document_avec_mrz()
        retour = rotate(rotate(rotate(rotate(image, 90), 90), 90), 90)
        assert np.array_equal(retour, image)

    @pytest.mark.parametrize("angle", [0, 90, 180, 270])
    def test_les_quatre_orientations_sont_proposees(self, angle: int):
        """Quelle que soit l'orientation de la photo, les quatre restent candidates.

        Les orientations de repli sont conservées : la détection morphologique produit
        des faux positifs, s'arrêter à la première « détectée » ferait rater la bonne.
        """
        contenu = encode(rotate(document_avec_mrz(), angle))
        candidats = preprocess_candidates(contenu)

        assert {c.rotation for c in candidats} == {0, 90, 180, 270}
        assert all(c.image.ndim == 2 and c.image.size > 0 for c in candidats)

    @pytest.mark.parametrize("angle", [0, 90, 180, 270])
    def test_une_orientation_redressant_le_mrz_est_essayee_en_premier(self, angle: int):
        """La bande MRZ redevient horizontale pour deux des quatre rotations.

        La détection doit en placer une en tête, sans quoi l'OCR paierait des passes
        inutiles avant de tomber sur la bonne orientation.
        """
        contenu = encode(rotate(document_avec_mrz(), angle))
        premier = preprocess_candidates(contenu)[0]

        assert premier.mrz_detected is True
        # Rotation totale subie par le document : le MRZ est horizontal si elle est
        # un multiple de 180 degrés.
        assert (angle + premier.rotation) % 180 == 0

    def test_lorientation_dorigine_est_essayee_en_premier(self):
        """Cas nominal : une photo droite ne doit pas coûter de passe OCR supplémentaire."""
        candidats = preprocess_candidates(encode(document_avec_mrz()))
        assert candidats[0].rotation == 0
        assert candidats[0].mrz_detected is True


class TestPipelineComplet:
    def test_le_pipeline_retourne_une_image_binaire_exploitable(self):
        result = preprocess_for_ocr(encode(document_avec_mrz()))

        assert result.image.ndim == 2
        assert result.image.size > 0
        assert set(np.unique(result.image)).issubset({0, 255})

    def test_repli_sur_la_bande_basse_quand_aucun_mrz_nest_detecte(self):
        uniforme = np.full((600, 900, 3), 200, dtype=np.uint8)
        result = preprocess_for_ocr(encode(uniforme))

        assert result.mrz_detected is False
        assert result.image.size > 0
