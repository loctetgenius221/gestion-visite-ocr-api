from __future__ import annotations

from typing import Annotated, Any, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer

from app.core.storage_urls import normalize_storage_url, sign_storage_url

T = TypeVar("T")

# URL de fichier **en sortie** : stockée nue, signée à la sérialisation JSON
# (ADR-024). `when_used="json"` : un `model_dump()` interne — journal d'audit,
# diff — garde le chemin stable, sans signature datée.
SignedStorageUrl = Annotated[
    str | None, PlainSerializer(sign_storage_url, return_type=str | None, when_used="json")
]

# URL de fichier **en entrée** : ramenée au chemin stocké, signature retirée.
StorageUrlInput = Annotated[str | None, AfterValidator(normalize_storage_url)]


class ORMModel(BaseModel):
    """Base des schémas de réponse construits à partir d'objets SQLAlchemy."""

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    """Format d'erreur standard (spec §5.6), identique sur toutes les routes."""

    error_code: str = Field(examples=["MRZ_NOT_DETECTED"])
    message: str = Field(examples=["Aucune zone MRZ n'a pu être détectée sur l'image fournie."])
    details: Any = None


class Page(BaseModel, Generic[T]):
    """Enveloppe de pagination (spec §7)."""

    items: list[T]
    total: int
    page: int
    page_size: int


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
