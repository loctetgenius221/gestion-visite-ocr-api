from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


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
