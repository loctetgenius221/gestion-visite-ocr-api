"""Preprocessing OpenCV de l'image de document avant OCR (spec §4.1).

Le pipeline vise un seul objectif : présenter à PaddleOCR une bande MRZ recadrée,
redressée et contrastée. Chaque étape retourne un `numpy.ndarray` et reste pure,
ce qui la rend testable indépendamment du moteur OCR.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from app.core.config import settings
from app.core.errors import UnsupportedImageError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Enregistre le décodeur HEIC auprès de Pillow (photos iPhone).
try:  # pragma: no cover - dépend de l'environnement d'exécution
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    logger.warning("pillow-heif indisponible : les images HEIC ne seront pas décodées.")

# Largeur de travail : au-delà, le gain de précision OCR ne compense plus le coût CPU.
_TARGET_WIDTH = 1600
# Une ligne MRZ est très allongée et court sur l'essentiel de la largeur du document ;
# le seuil de couverture reste tolérant pour absorber les marges d'une photo cadrée à la main.
_MIN_MRZ_ASPECT_RATIO = 4.0
_MIN_MRZ_WIDTH_COVERAGE = 0.5

# Format ID-1 (ISO/IEC 7810) : 85,6 × 54 mm, soit un ratio largeur/hauteur de 1,585.
# La tolérance absorbe la déformation de perspective d'une photo prise à la main.
_CARD_ASPECT_RATIO = 85.6 / 54.0
_CARD_RATIO_MIN = 1.35
_CARD_RATIO_MAX = 1.85
_MIN_CARD_AREA_RATIO = 0.10
_MAX_CARD_AREA_RATIO = 0.97

# Dimensions de la carte redressée. 1300 px de large est le point d'équilibre mesuré
# sur photo réelle : en dessous la lecture du NIN devient instable, au-dessus le coût
# OCR grimpe sans gain de fiabilité.
_CARD_CANONICAL_WIDTH = 1300
_CARD_CANONICAL_HEIGHT = int(_CARD_CANONICAL_WIDTH / _CARD_ASPECT_RATIO)

# Marge remontée au-dessus de la bande MRZ pour englober la ligne NIN imprimée.
#
# 0,10 était trop juste : sur une carte redressée de 820 px de haut, cela ne
# laissait que ~82 px au-dessus du MRZ, soit à peine la hauteur d'une ligne de
# texte. Selon le cadrage, la ligne NIN tombait hors de la zone envoyée à l'OCR et
# le champ revenait vide. 0,20 la capture avec une marge confortable ; le bruit
# supplémentaire est sans conséquence, `extract_nin` filtrant par libellé et par
# structure.
_NIN_MARGIN_RATIO = 0.20
# Sans détection morphologique, on retient tout le bas de la carte.
_FALLBACK_BOTTOM_RATIO = 0.50


@dataclass(frozen=True, slots=True)
class PreprocessedImage:
    """Résultat du preprocessing : la bande MRZ si détectée, sinon l'image entière."""

    image: np.ndarray
    mrz_detected: bool
    rotation: int = 0


# Une photo prise au téléphone peut arriver dans n'importe quelle orientation : la
# carte est souvent tenue en portrait alors que le MRZ, lui, est en paysage. Les
# métadonnées EXIF ne sont pas fiables (souvent absentes après recadrage côté mobile),
# on teste donc les quatre quarts de tour.
_ORIENTATIONS: tuple[int, ...] = (0, 90, 270, 180)

_ROTATION_CODES = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def rotate(image: np.ndarray, angle: int) -> np.ndarray:
    """Rotation par quart de tour, sans interpolation ni perte."""
    code = _ROTATION_CODES.get(angle)
    return image if code is None else cv2.rotate(image, code)


def decode_image(content: bytes) -> np.ndarray:
    """Décode des octets en image BGR OpenCV, en convertissant le HEIC si nécessaire."""
    array = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is not None:
        return image

    # OpenCV ne lit pas le HEIC : on repasse par Pillow avant de revenir en BGR.
    try:
        with Image.open(io.BytesIO(content)) as pil_image:
            rgb = np.array(pil_image.convert("RGB"))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception as exc:
        raise UnsupportedImageError("L'image fournie n'a pas pu être décodée.") from exc


def resize_to_working_width(image: np.ndarray, target_width: int = _TARGET_WIDTH) -> np.ndarray:
    height, width = image.shape[:2]
    if width <= target_width:
        return image
    scale = target_width / width
    return cv2.resize(
        image, (target_width, int(height * scale)), interpolation=cv2.INTER_AREA
    )


def to_grayscale(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def estimate_skew_angle(gray: np.ndarray) -> float:
    """Estime l'inclinaison du texte, en degrés, via la boîte englobante minimale."""
    inverted = cv2.bitwise_not(gray)
    _, binary = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    coords = cv2.findNonZero(binary)
    if coords is None or len(coords) < 10:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    # OpenCV renvoie l'angle dans ]0, 90] : on le ramène dans [-45, 45].
    if angle > 45:
        angle -= 90
    return float(angle)


def deskew(image: np.ndarray, max_angle: float = 20.0) -> np.ndarray:
    """Redresse l'image si un angle significatif mais plausible est détecté."""
    angle = estimate_skew_angle(to_grayscale(image))
    if abs(angle) < 0.5 or abs(angle) > max_angle:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def enhance_contrast(gray: np.ndarray) -> np.ndarray:
    """CLAHE : égalise localement le contraste, robuste aux photos mal éclairées."""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def binarize(gray: np.ndarray) -> np.ndarray:
    """Binarisation adaptative, adaptée aux gradients d'éclairage d'une photo au smartphone."""
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 15
    )


def detect_mrz_region(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Localise la bande MRZ par morphologie (blackhat + fermeture horizontale).

    Le MRZ est du texte sombre, monospace et très dense horizontalement : un noyau
    rectangulaire large le fusionne en un blob unique, très allongé, que l'on
    distingue du reste du document par son rapport largeur/hauteur.
    """
    height, width = gray.shape[:2]
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)

    rect_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(13, width // 60), 5))
    square_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))

    blackhat = cv2.morphologyEx(blurred, cv2.MORPH_BLACKHAT, rect_kernel)
    grad = cv2.Sobel(blackhat, ddepth=cv2.CV_32F, dx=1, dy=0, ksize=-1)
    grad = np.absolute(grad)
    min_val, max_val = float(np.min(grad)), float(np.max(grad))
    if max_val - min_val < 1e-6:
        return None
    grad = ((grad - min_val) / (max_val - min_val) * 255).astype("uint8")

    grad = cv2.morphologyEx(grad, cv2.MORPH_CLOSE, rect_kernel)
    _, thresh = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, square_kernel)
    thresh = cv2.erode(thresh, None, iterations=4)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[int, int, int, int]] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
        x, y, w, h = cv2.boundingRect(contour)
        if h == 0:
            continue
        # Le MRZ occupe une large fraction de la largeur, dans la moitié basse du document.
        if (
            w / h >= _MIN_MRZ_ASPECT_RATIO
            and w / width >= _MIN_MRZ_WIDTH_COVERAGE
            and (y + h) >= height * settings.OCR_MRZ_BAND_TOP_RATIO * 0.8
        ):
            candidates.append((x, y, w, h))

    if not candidates:
        return None

    # Chaque ligne du MRZ ressort souvent comme un blob distinct (l'interligne dépasse
    # le noyau de fermeture) : on prend l'union pour retrouver la bande complète, sans
    # quoi seule une des 2 ou 3 lignes serait transmise à l'OCR.
    left = min(box[0] for box in candidates)
    top = min(box[1] for box in candidates)
    right = max(box[0] + box[2] for box in candidates)
    bottom = max(box[1] + box[3] for box in candidates)
    x, y, w, h = left, top, right - left, bottom - top

    # Marge de sécurité : le contour érodé rogne souvent la première/dernière ligne.
    pad_x, pad_y = int(width * 0.02), int(h * 0.35)
    x = max(0, x - pad_x)
    y = max(0, y - pad_y)
    w = min(width - x, w + 2 * pad_x)
    h = min(height - y, h + 2 * pad_y)
    return x, y, w, h


def order_corners(points: np.ndarray) -> np.ndarray:
    """Ordonne 4 coins en haut-gauche, haut-droit, bas-droit, bas-gauche.

    La somme des coordonnées est minimale en haut-gauche et maximale en bas-droit ;
    leur différence sépare les deux autres.
    """
    points = points.reshape(4, 2).astype("float32")
    somme = points.sum(axis=1)
    diff = np.diff(points, axis=1).ravel()
    return np.array(
        [
            points[np.argmin(somme)],
            points[np.argmin(diff)],
            points[np.argmax(somme)],
            points[np.argmax(diff)],
        ],
        dtype="float32",
    )


def detect_card(image: np.ndarray) -> np.ndarray | None:
    """Localise les 4 coins de la carte d'identité dans la photo.

    Une CNI est un rectangle clair posé sur un fond généralement plus sombre : un
    seuillage d'Otsu l'isole, et le plus grand contour à 4 sommets est retenu s'il
    respecte les proportions du format ID-1 (85,6 × 54 mm, soit un ratio de ~1,585).

    Ce contrôle de proportions est essentiel : sans lui, le cadre de la photo ou une
    feuille de papier posée à côté seraient pris pour la carte.
    """
    height, width = image.shape[:2]
    gray = cv2.GaussianBlur(to_grayscale(image), (7, 7), 0)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area_ratio = cv2.contourArea(contour) / (height * width)
        if not _MIN_CARD_AREA_RATIO <= area_ratio <= _MAX_CARD_AREA_RATIO:
            continue

        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) != 4:
            continue

        corners = order_corners(approx)
        top_left, top_right, bottom_right, bottom_left = corners
        largeur = max(
            np.linalg.norm(top_right - top_left),
            np.linalg.norm(bottom_right - bottom_left),
        )
        hauteur = max(
            np.linalg.norm(bottom_left - top_left),
            np.linalg.norm(bottom_right - top_right),
        )
        if hauteur < 1:
            continue

        ratio = largeur / hauteur
        # La carte peut être photographiée en portrait : on accepte aussi son inverse.
        if _CARD_RATIO_MIN <= ratio <= _CARD_RATIO_MAX or (
            _CARD_RATIO_MIN <= 1 / ratio <= _CARD_RATIO_MAX
        ):
            return corners

    return None


def rectify_card(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Redresse la carte en vue de dessus, aux proportions ID-1 normalisées.

    La correction de perspective supprime l'inclinaison de la prise de vue et ramène
    toutes les photos à un référentiel unique, ce qui permet ensuite de cibler le MRZ
    et la ligne NIN par de simples ratios de hauteur.
    """
    cible = np.array(
        [
            [0, 0],
            [_CARD_CANONICAL_WIDTH - 1, 0],
            [_CARD_CANONICAL_WIDTH - 1, _CARD_CANONICAL_HEIGHT - 1],
            [0, _CARD_CANONICAL_HEIGHT - 1],
        ],
        dtype="float32",
    )
    matrice = cv2.getPerspectiveTransform(corners, cible)
    redressee = cv2.warpPerspective(
        image, matrice, (_CARD_CANONICAL_WIDTH, _CARD_CANONICAL_HEIGHT)
    )
    # Carte photographiée en portrait : on la remet en paysage.
    if redressee.shape[0] > redressee.shape[1]:
        redressee = cv2.rotate(redressee, cv2.ROTATE_90_CLOCKWISE)
    return redressee


def extract_card(image: np.ndarray) -> np.ndarray | None:
    """Détecte puis redresse la carte ; `None` si aucune carte plausible."""
    corners = detect_card(image)
    if corners is None:
        return None
    return rectify_card(image, corners)


def preprocess_for_ocr(content: bytes) -> PreprocessedImage:
    """Chaîne complète sur l'orientation d'origine de l'image.

    Pour une photo dont l'orientation n'est pas garantie, préférer
    `preprocess_candidates`, qui couvre les quatre quarts de tour.
    """
    return _preprocess_oriented(resize_to_working_width(decode_image(content)))


def _preprocess_oriented(image: np.ndarray, rotation: int = 0) -> PreprocessedImage:
    """Décodage déjà fait : redressement → détection MRZ → contraste → binarisation."""
    image = deskew(image)
    gray = to_grayscale(image)

    region = detect_mrz_region(gray)
    if region is not None:
        x, y, w, h = region
        gray = gray[y : y + h, x : x + w]
        mrz_detected = True
    else:
        # Repli : on garde la bande basse du document, où le MRZ se trouve par construction.
        # Si l'app Flutter impose un cadre de capture guidé, ce repli suffit à lui seul.
        logger.info("Zone MRZ non localisée par morphologie, repli sur la bande basse.")
        top = int(gray.shape[0] * settings.OCR_MRZ_BAND_TOP_RATIO)
        gray = gray[top:, :]
        mrz_detected = False

    if gray.size == 0:
        raise UnsupportedImageError("L'image fournie est trop petite pour être analysée.")

    gray = enhance_contrast(gray)
    # Agrandir la bande améliore nettement la reconnaissance des petits caractères MRZ.
    if gray.shape[1] < 1000:
        scale = 1000 / gray.shape[1]
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    return PreprocessedImage(image=binarize(gray), mrz_detected=mrz_detected, rotation=rotation)


def _bottom_zone(card: np.ndarray, rotation: int) -> PreprocessedImage:
    """Isole le bas de la carte redressée : ligne NIN imprimée **et** bande MRZ.

    Les deux informations tiennent dans une seule bande, donc dans **une seule passe
    OCR** : le NIN est imprimé juste au-dessus du MRZ sur les CNI sénégalaises.
    """
    height = card.shape[0]
    gray = to_grayscale(card)

    region = detect_mrz_region(gray)
    if region is not None:
        # La bande morphologique s'arrête au dernier trait détecté et ampute souvent
        # la 3e ligne : on l'étend jusqu'au bord bas, et vers le haut pour englober
        # la ligne NIN.
        top = max(0, region[1] - int(_NIN_MARGIN_RATIO * height))
        detected = True
    else:
        top = int(height * _FALLBACK_BOTTOM_RATIO)
        detected = False

    zone = enhance_contrast(gray[top:height, :])
    return PreprocessedImage(image=binarize(zone), mrz_detected=detected, rotation=rotation)


def preprocess_candidates(content: bytes) -> list[PreprocessedImage]:
    """Prépare l'image pour l'OCR, dans l'ordre des hypothèses les plus probables.

    Deux stratégies selon que la carte est localisée ou non :

    - **carte détectée** — la perspective est corrigée et l'orientation ramenée en
      paysage : il ne reste que l'ambiguïté haut/bas, soit deux candidats au plus ;
    - **carte non détectée** (cadrage serré, fond de même teinte…) — on retombe sur
      l'image entière et les quatre quarts de tour.
    """
    base = resize_to_working_width(decode_image(content))

    card = extract_card(base)
    if card is not None:
        logger.info("Carte détectée et redressée", extra={"shape": list(card.shape[:2])})
        return [_bottom_zone(card, rotation=0), _bottom_zone(rotate(card, 180), rotation=180)]

    logger.info("Aucune carte localisée, repli sur l'image entière.")
    detectees: list[PreprocessedImage] = []
    replis: list[PreprocessedImage] = []
    for angle in _ORIENTATIONS:
        try:
            result = _preprocess_oriented(rotate(base, angle), rotation=angle)
        except UnsupportedImageError:
            continue
        (detectees if result.mrz_detected else replis).append(result)

    # Les replis restent dans la liste : la détection morphologique produit des faux
    # positifs (une texture de fond peut passer pour une bande), et s'arrêter à la
    # première orientation « détectée » ferait manquer la bonne. Le surcoût est nul
    # dans le cas nominal, l'appelant s'arrêtant dès qu'un MRZ se parse.
    return detectees + replis
