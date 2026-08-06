from __future__ import annotations

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    """URL d'un fichier déposé, à reporter dans le payload qui le référence."""

    url: str = Field(
        description="Chemin relatif au serveur, à préfixer par l'URL de base côté client.",
        examples=["/storage/uploads/signatures/2026/08/8f2c1e....png"],
    )
