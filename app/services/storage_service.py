"""Abstraction du stockage de fichiers (images MRZ, signatures).

L'interface `StorageService` isole le reste de l'application du support physique :
seul `LocalStorageService` existe aujourd'hui, mais une implémentation S3 peut être
branchée sans toucher aux routers ni aux services métier (spec §2).
"""

from __future__ import annotations

import io
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import anyio
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.errors import FileTooLargeError, UnsupportedImageError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Enregistre le décodeur HEIC auprès de Pillow (photos iPhone).
try:  # pragma: no cover - dépend de l'environnement d'exécution
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    logger.warning("pillow-heif indisponible : les images HEIC ne seront pas décodées.")

ALLOWED_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif"})

# Formats acceptés pour une photo de pièce ou une signature, d'après le
# **contenu**. MPO est le JPEG « multi-images » de certains téléphones.
FORMATS_PHOTO = frozenset({"JPEG", "MPO", "PNG", "HEIF"})

# Formats acceptés pour un logo, décidés d'après le **contenu** du fichier et non
# son extension. SVG en est volontairement absent : c'est un document XML, qui
# peut porter du script et s'exécute quand le navigateur l'affiche autrement que
# dans une balise `img`.
FORMATS_LOGO = frozenset({"PNG", "JPEG", "WEBP", "GIF"})


def validate_image_upload(filename: str | None, content: bytes) -> str:
    """Valide extension et taille d'une image uploadée, et retourne l'extension normalisée."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise UnsupportedImageError(
            details={"filename": filename, "allowed": sorted(ALLOWED_IMAGE_EXTENSIONS)}
        )
    if len(content) > settings.max_upload_size_bytes:
        raise FileTooLargeError(
            details={"max_mb": settings.MAX_UPLOAD_SIZE_MB, "received_bytes": len(content)}
        )
    if not content:
        raise UnsupportedImageError("Le fichier envoyé est vide.")
    return suffix


def inspecter_image(content: bytes, formats: frozenset[str] = FORMATS_PHOTO) -> str:
    """Vérifie le format réel et les dimensions d'une image, **sans la décoder**.

    `Image.open` ne lit que l'en-tête : c'est ce qui permet de refuser une bombe
    de décompression avant qu'elle n'occupe la mémoire (audit SEC-008).
    Retourne le format détecté.
    """
    try:
        with Image.open(io.BytesIO(content)) as image:
            format_source = (image.format or "").upper()
            largeur, hauteur = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise UnsupportedImageError("Ce fichier n'est pas une image exploitable.") from exc
    except Image.DecompressionBombError as exc:
        raise FileTooLargeError(
            "L'image dépasse le nombre de pixels autorisé.",
            details={"max_megapixels": settings.MAX_IMAGE_MEGAPIXELS},
        ) from exc

    if format_source not in formats:
        raise UnsupportedImageError(
            details={"format": format_source or None, "acceptes": sorted(formats)}
        )
    if largeur * hauteur > settings.MAX_IMAGE_MEGAPIXELS * 1_000_000:
        raise FileTooLargeError(
            "L'image dépasse le nombre de pixels autorisé.",
            details={
                "max_megapixels": settings.MAX_IMAGE_MEGAPIXELS,
                "largeur": largeur,
                "hauteur": hauteur,
            },
        )
    return format_source


SortiePhoto = Literal["JPEG", "PNG"]
_EXTENSIONS_SORTIE: dict[str, str] = {"JPEG": ".jpg", "PNG": ".png"}


def normaliser_photo(content: bytes, sortie: SortiePhoto) -> tuple[bytes, str]:
    """Vérifie qu'une photo ou une signature est une image, puis la réencode.

    Même principe que `normalize_logo` (audit SEC-007). Un fichier nommé `.png`
    était stocké tel quel, quel que soit son contenu, puis servi par nginx. Le
    réencodage à partir des seuls pixels décodés retire aussi les métadonnées,
    dont la position GPS de la tablette dans l'EXIF. L'orientation EXIF est
    appliquée aux pixels avant de disparaître, sans quoi la photo s'afficherait
    couchée.

    `JPEG` pour les photos de pièce, y compris le HEIC, que les navigateurs du
    dashboard n'affichent pas. `PNG` pour les signatures, qui gardent leur
    transparence. Retourne les octets et l'extension à stocker. Opération CPU :
    à appeler hors de la boucle asyncio.
    """
    inspecter_image(content)
    try:
        with Image.open(io.BytesIO(content)) as image:
            redressee = ImageOps.exif_transpose(image)
            if sortie == "JPEG":
                pixels = redressee.convert("RGB")
                options: dict[str, object] = {"quality": 90, "optimize": True}
            else:
                pixels = redressee.convert("RGBA")
                options = {"optimize": True}
            tampon = io.BytesIO()
            pixels.save(tampon, format=sortie, **options)
    except OSError as exc:
        # Image tronquée ou corrompue : Pillow échoue au décodage, pas à l'ouverture.
        raise UnsupportedImageError("Cette image n'a pas pu être décodée.") from exc
    return tampon.getvalue(), _EXTENSIONS_SORTIE[sortie]


def normalize_logo(content: bytes) -> bytes:
    """Vérifie qu'un logo est bien une image, et le réencode en PNG borné.

    Le contrôle porte sur le **contenu**, pas sur le nom du fichier. Un `.png`
    n'est qu'une extension : un document HTML renommé passait la validation
    précédente, était stocké, puis servi par `StaticFiles` depuis l'origine même
    du dashboard — soit un script exécuté avec la session de l'administrateur qui
    l'ouvre. Pillow refuse ici tout ce qui n'est pas une image qu'il sait décoder.

    Le réencodage n'est pas cosmétique non plus : il reconstruit le fichier à
    partir des seuls pixels décodés, ce qui laisse tomber les métadonnées — EXIF,
    profils, blocs de commentaire — où l'octet arbitraire se cache d'ordinaire.

    Retourne les octets PNG à stocker.
    """
    if not content:
        raise UnsupportedImageError("Le fichier envoyé est vide.")
    if len(content) > settings.max_logo_size_bytes:
        raise FileTooLargeError(
            details={"max_mb": settings.MAX_LOGO_SIZE_MB, "received_bytes": len(content)}
        )

    try:
        with Image.open(io.BytesIO(content)) as image:
            format_source = (image.format or "").upper()
            if format_source not in FORMATS_LOGO:
                raise UnsupportedImageError(
                    "Format de logo non supporté.",
                    details={"format": format_source or None, "acceptes": sorted(FORMATS_LOGO)},
                )
            # `RGBA` systématiquement : un logo est presque toujours détouré, et
            # aplatir sa transparence sur du blanc le rend inutilisable sur un
            # fond sombre.
            normalisee = image.convert("RGBA")
            cote = settings.LOGO_MAX_DIMENSION_PX
            if max(normalisee.size) > cote:
                normalisee.thumbnail((cote, cote), Image.Resampling.LANCZOS)

            sortie = io.BytesIO()
            normalisee.save(sortie, format="PNG", optimize=True)
    except UnidentifiedImageError as exc:
        raise UnsupportedImageError(
            "Ce fichier n'est pas une image exploitable."
        ) from exc
    except OSError as exc:
        # Image tronquée ou corrompue : Pillow échoue au décodage, pas à l'ouverture.
        raise UnsupportedImageError("Cette image n'a pas pu être décodée.") from exc

    return sortie.getvalue()


class StorageService(ABC):
    """Contrat de stockage : sauvegarde un blob et retourne son URL/chemin public."""

    @abstractmethod
    async def save(self, content: bytes, *, folder: str, extension: str) -> str: ...

    @abstractmethod
    async def delete(self, url: str) -> None: ...


class LocalStorageService(StorageService):
    """Stockage sur disque local, organisé par `folder/AAAA/MM/`."""

    def __init__(self, base_dir: str | None = None, public_base_url: str | None = None) -> None:
        self.base_dir = Path(base_dir or settings.STORAGE_DIR)
        self.public_base_url = (public_base_url or settings.STORAGE_PUBLIC_BASE_URL).rstrip("/")

    async def save(self, content: bytes, *, folder: str, extension: str) -> str:
        now = datetime.now(UTC)
        relative_dir = Path(folder) / f"{now:%Y}" / f"{now:%m}"
        target_dir = self.base_dir / relative_dir
        await anyio.to_thread.run_sync(lambda: target_dir.mkdir(parents=True, exist_ok=True))

        filename = f"{uuid.uuid4().hex}{extension}"
        target = target_dir / filename
        await anyio.Path(target).write_bytes(content)
        return f"{self.public_base_url}/{relative_dir.as_posix()}/{filename}"

    async def delete(self, url: str) -> None:
        relative = url.removeprefix(self.public_base_url).lstrip("/")
        target = self.base_dir / relative
        # Garde-fou anti path traversal : on refuse toute cible hors du répertoire de base.
        try:
            target.resolve().relative_to(self.base_dir.resolve())
        except ValueError:
            return
        if await anyio.Path(target).exists():
            await anyio.Path(target).unlink()


_storage_service: StorageService | None = None


def get_storage_service() -> StorageService:
    """Dépendance FastAPI : singleton du service de stockage."""
    global _storage_service
    if _storage_service is None:
        _storage_service = LocalStorageService()
    return _storage_service
