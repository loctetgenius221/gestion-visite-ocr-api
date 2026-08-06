"""Abstraction du stockage de fichiers (images MRZ, signatures).

L'interface `StorageService` isole le reste de l'application du support physique :
seul `LocalStorageService` existe aujourd'hui, mais une implémentation S3 peut être
branchée sans toucher aux routers ni aux services métier (spec §2).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

import anyio

from app.core.config import settings
from app.core.errors import FileTooLargeError, UnsupportedImageError

ALLOWED_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif"})


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
