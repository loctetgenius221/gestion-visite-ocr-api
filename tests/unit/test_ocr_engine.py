"""Tests de la normalisation des sorties PaddleOCR.

Le modèle lui-même n'est pas chargé : on valide uniquement le décodage des deux
formats de sortie que la lib a connus, et le tri vertical des lignes.
"""

from __future__ import annotations

import numpy as np

from app.services.ocr_engine import _extract_lines, to_bgr


def poly(y: float) -> list[list[float]]:
    """Quadrilatère de détection à l'ordonnée `y`."""
    return [[0.0, y], [100.0, y], [100.0, y + 10], [0.0, y + 10]]


class TestFormatModerne:
    def test_lignes_extraites_dans_lordre_vertical(self):
        page = {
            "rec_texts": ["LIGNE_BASSE", "LIGNE_HAUTE"],
            "dt_polys": [poly(200), poly(50)],
        }
        assert _extract_lines([page]) == ["LIGNE_HAUTE", "LIGNE_BASSE"]

    def test_sortie_sans_polygones_conserve_lordre_dorigine(self):
        page = {"rec_texts": ["A", "B"], "dt_polys": []}
        assert _extract_lines([page]) == ["A", "B"]

    def test_page_sans_texte(self):
        assert _extract_lines([{"rec_texts": [], "dt_polys": []}]) == []


class TestFormatHistorique:
    def test_lignes_extraites_et_triees(self):
        page = [[poly(120), ("SECONDE", 0.98)], [poly(30), ("PREMIERE", 0.97)]]
        assert _extract_lines([page]) == ["PREMIERE", "SECONDE"]

    def test_entree_malformee_est_ignoree_sans_planter(self):
        page = [[poly(30), ("VALIDE", 0.9)], "entrée corrompue", None]
        assert _extract_lines([page]) == ["VALIDE"]


class TestCasLimites:
    def test_sortie_vide(self):
        assert _extract_lines([]) == []

    def test_sortie_nulle(self):
        assert _extract_lines(None) == []


class TestConversionEnTroisCanaux:
    """PaddleOCR refuse les tableaux 2D : le preprocessing produit du niveau de gris.

    Sans cette conversion, `predict` lève `not enough values to unpack (expected 3)`.
    """

    def test_une_image_en_niveaux_de_gris_devient_bgr(self):
        gris = np.full((40, 60), 128, dtype=np.uint8)

        converti = to_bgr(gris)

        assert converti.shape == (40, 60, 3)
        assert (converti[..., 0] == 128).all()

    def test_un_canal_unique_explicite_est_developpe(self):
        converti = to_bgr(np.full((40, 60, 1), 200, dtype=np.uint8))
        assert converti.shape == (40, 60, 3)

    def test_une_image_deja_en_couleur_est_inchangee(self):
        couleur = np.zeros((40, 60, 3), dtype=np.uint8)
        assert to_bgr(couleur) is couleur
