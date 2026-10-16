from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import SignedStorageUrl


class UploadResponse(BaseModel):
    """URL d'un fichier déposé, à reporter dans le payload qui le référence."""

    url: SignedStorageUrl = Field(
        description=(
            "Chemin relatif au serveur, à préfixer par l'URL de base côté client. "
            "En production, il porte une signature datée (`md5`, `expires`) : "
            "l'afficher tel quel, et le renvoyer tel quel dans le payload qui le "
            "référence — le serveur retire la signature."
        ),
        examples=["/storage/uploads/org/<organisation>/signatures/2026/08/8f2c1e....png"],
    )
