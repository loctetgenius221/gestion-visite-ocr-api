"""Wrapper autour de PaddleOCR.

Le modèle est lourd à charger (plusieurs secondes) : il est instancié **une seule
fois** et conservé en mémoire, idéalement préchargé au démarrage via le lifespan de
l'application (spec §4.3 : budget de 3 s par requête).
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

import cv2
import numpy as np

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class OcrEngine(Protocol):
    """Contrat minimal attendu par le pipeline : image → lignes de texte."""

    def read_lines(self, image: np.ndarray) -> list[str]: ...


def to_bgr(image: np.ndarray) -> np.ndarray:
    """Convertit une image en 3 canaux : PaddleOCR rejette les tableaux 2D.

    Le preprocessing produit du niveau de gris binarisé ; sans cette conversion,
    `predict` lève `ValueError: not enough values to unpack`.
    """
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.ndim == 3 and image.shape[2] == 1:
        return cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
    return image


class PaddleOcrEngine:
    """Implémentation PaddleOCR, chargée paresseusement et protégée par un verrou."""

    def __init__(self) -> None:
        self._ocr: Any | None = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """Charge le modèle. Idempotent, sûr en concurrence."""
        if self._ocr is not None:
            return
        with self._lock:
            if self._ocr is not None:
                return
            from paddleocr import PaddleOCR

            logger.info("Chargement du modèle PaddleOCR", extra={"lang": settings.OCR_LANG})
            # Le MRZ est une bande de texte déjà recadrée et redressée en amont par
            # OpenCV : les modules d'orientation/dewarping sont désactivés, ils
            # coûtent cher et n'apportent rien ici.
            self._ocr = PaddleOCR(
                lang=settings.OCR_LANG,
                text_detection_model_name=settings.OCR_DET_MODEL,
                text_recognition_model_name=settings.OCR_REC_MODEL,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="gpu" if settings.OCR_USE_GPU else "cpu",
                # Le backend oneDNN de PaddlePaddle 3.3 sur Windows lève
                # `ConvertPirAttribute2RuntimeAttribute not support` sur toute
                # inférence. Désactivé par défaut ; réactivable par variable
                # d'environnement si une version corrigée est déployée.
                enable_mkldnn=settings.OCR_ENABLE_MKLDNN,
            )
            logger.info("Modèle PaddleOCR chargé")

    def read_lines(self, image: np.ndarray) -> list[str]:
        """Retourne les textes détectés, ordonnés de haut en bas."""
        self.load()
        assert self._ocr is not None
        raw = self._ocr.predict(to_bgr(image))
        return _extract_lines(raw)


def _extract_lines(raw: Any) -> list[str]:
    """Normalise la sortie de PaddleOCR, dont le format varie selon la version.

    PaddleOCR >= 3 renvoie une liste de dicts (`rec_texts` + `dt_polys`) ; les
    versions antérieures renvoyaient une liste de `[box, (texte, score)]`.
    """
    lines: list[tuple[float, str]] = []
    if not raw:
        return []

    for page in raw:
        if isinstance(page, dict) or hasattr(page, "get"):
            texts = page.get("rec_texts") or []
            polys = page.get("dt_polys") or page.get("rec_polys") or []
            for index, text in enumerate(texts):
                y = 0.0
                if index < len(polys):
                    poly = np.asarray(polys[index], dtype=float).reshape(-1, 2)
                    y = float(poly[:, 1].mean())
                lines.append((y, str(text)))
        else:
            for entry in page or []:
                try:
                    box, (text, _score) = entry[0], entry[1]
                    y = float(np.asarray(box, dtype=float).reshape(-1, 2)[:, 1].mean())
                    lines.append((y, str(text)))
                except (TypeError, ValueError, IndexError):
                    continue

    lines.sort(key=lambda item: item[0])
    return [text for _, text in lines]


_engine: PaddleOcrEngine | None = None


def get_ocr_engine() -> PaddleOcrEngine:
    """Singleton du moteur OCR."""
    global _engine
    if _engine is None:
        _engine = PaddleOcrEngine()
    return _engine
