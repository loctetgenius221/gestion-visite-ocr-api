"""Tests de la détection et du redressement de la carte dans la photo.

Sur une photo prise à la main, la carte n'occupe qu'une partie du cadre et subit une
déformation de perspective. La localiser puis la redresser ramène toutes les photos à
un référentiel unique, où le MRZ et la ligne NIN se ciblent par simples ratios.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.services.image_preprocessing import (
    detect_card,
    extract_card,
    order_corners,
    preprocess_candidates,
)

# Proportions du format ID-1 (85,6 × 54 mm).
RATIO_ID1 = 85.6 / 54.0


def photo_de_carte(
    largeur_photo: int = 1600,
    hauteur_photo: int = 1200,
    largeur_carte: int = 900,
    decalage: tuple[int, int] = (300, 250),
    perspective: int = 0,
) -> np.ndarray:
    """Photo synthétique : carte claire posée sur un fond sombre texturé."""
    fond = np.random.default_rng(7).integers(
        60, 110, (hauteur_photo, largeur_photo, 3), dtype=np.uint8
    )

    hauteur_carte = int(largeur_carte / RATIO_ID1)
    carte = np.full((hauteur_carte, largeur_carte, 3), 245, dtype=np.uint8)
    cv2.putText(
        carte, "INFORMATIONS ELECTORALES", (40, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (60, 140, 80), 2
    )
    cv2.putText(
        carte, "NIN 1 895 2003 00511", (200, 330), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2
    )

    x, y = decalage
    if perspective:
        # Incline le bord supérieur pour simuler une prise de vue de biais.
        source = np.float32(
            [[0, 0], [largeur_carte, 0], [largeur_carte, hauteur_carte], [0, hauteur_carte]]
        )
        cible = np.float32(
            [
                [perspective, 0],
                [largeur_carte - perspective, 0],
                [largeur_carte, hauteur_carte],
                [0, hauteur_carte],
            ]
        )
        carte = cv2.warpPerspective(
            carte, cv2.getPerspectiveTransform(source, cible), (largeur_carte, hauteur_carte)
        )
    fond[y : y + hauteur_carte, x : x + largeur_carte] = carte
    return fond


class TestOrdreDesCoins:
    def test_les_coins_sont_ordonnes_dans_le_sens_horaire(self):
        desordre = np.array([[100, 300], [400, 50], [100, 50], [400, 300]], dtype="float32")

        haut_g, haut_d, bas_d, bas_g = order_corners(desordre)

        assert tuple(haut_g) == (100, 50)
        assert tuple(haut_d) == (400, 50)
        assert tuple(bas_d) == (400, 300)
        assert tuple(bas_g) == (100, 300)


class TestDetection:
    def test_la_carte_est_localisee_sur_un_fond_sombre(self):
        coins = detect_card(photo_de_carte())

        assert coins is not None
        assert coins.shape == (4, 2)

    def test_les_coins_encadrent_la_carte(self):
        coins = detect_card(photo_de_carte(decalage=(300, 250), largeur_carte=900))

        xs, ys = coins[:, 0], coins[:, 1]
        # Tolérance de quelques pixels : le seuillage rogne légèrement les bords.
        assert 290 <= xs.min() <= 315
        assert 240 <= ys.min() <= 265

    def test_une_carte_en_perspective_est_localisee(self):
        assert detect_card(photo_de_carte(perspective=60)) is not None

    def test_un_fond_uniforme_sans_carte_ne_donne_rien(self):
        uniforme = np.full((1200, 1600, 3), 200, dtype=np.uint8)
        assert detect_card(uniforme) is None

    def test_une_forme_aux_mauvaises_proportions_est_rejetee(self):
        """Une feuille de papier posée à côté ne doit pas être prise pour la carte."""
        photo = np.full((1200, 1600, 3), 70, dtype=np.uint8)
        photo[200:1000, 600:1000] = 245  # rectangle très vertical, ratio 0.5

        assert detect_card(photo) is None


class TestRedressement:
    def test_la_carte_redressee_a_les_proportions_id1(self):
        carte = extract_card(photo_de_carte())

        assert carte is not None
        hauteur, largeur = carte.shape[:2]
        assert largeur / hauteur == pytest.approx(RATIO_ID1, abs=0.02)

    def test_le_fond_a_disparu_du_resultat(self):
        """La carte redressée ne doit plus contenir le fond sombre de la photo."""
        carte = extract_card(photo_de_carte())

        assert carte is not None
        # Le fond synthétique est sombre (60-110) ; la carte est claire (245).
        assert carte.mean() > 180

    def test_la_perspective_est_corrigee(self):
        carte = extract_card(photo_de_carte(perspective=60))

        assert carte is not None
        hauteur, largeur = carte.shape[:2]
        assert largeur / hauteur == pytest.approx(RATIO_ID1, abs=0.02)

    def test_une_carte_photographiee_en_portrait_est_remise_en_paysage(self):
        photo = photo_de_carte()
        portrait = cv2.rotate(photo, cv2.ROTATE_90_CLOCKWISE)

        carte = extract_card(portrait)

        assert carte is not None
        assert carte.shape[1] > carte.shape[0]


class TestCandidatsAvecCarte:
    def test_deux_candidats_seulement_quand_la_carte_est_trouvee(self):
        """Perspective corrigée : il ne reste que l'ambiguïté haut/bas, pas 4 rotations."""
        ok, buffer = cv2.imencode(".jpg", photo_de_carte())
        assert ok

        candidats = preprocess_candidates(buffer.tobytes())

        assert [c.rotation for c in candidats] == [0, 180]

    def test_repli_sur_quatre_orientations_sans_carte_detectee(self):
        # Document occupant tout le cadre : aucun fond, donc aucune carte à découper.
        plein_cadre = np.full((600, 900, 3), 240, dtype=np.uint8)
        cv2.putText(
            plein_cadre,
            "I<SEN101200302<0100",
            (20, 500),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
        )
        ok, buffer = cv2.imencode(".jpg", plein_cadre)
        assert ok

        candidats = preprocess_candidates(buffer.tobytes())

        assert {c.rotation for c in candidats} == {0, 90, 180, 270}
