from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, UploadFile, status

from app.core.deps import CurrentUser, StorageDep
from app.schemas.upload import UploadResponse
from app.services.storage_service import validate_image_upload

router = APIRouter(prefix="/uploads", tags=["Fichiers"])


@router.post(
    "/signature",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dépose la signature manuscrite du visiteur",
)
async def upload_signature(
    storage: StorageDep,
    current_user: CurrentUser,
    signature: Annotated[UploadFile, File(description="Signature manuscrite (PNG de préférence).")],
) -> UploadResponse:
    """Stocke l'image de signature et retourne son URL.

    L'app mobile capture la signature sur un pad tactile : elle la dépose ici, puis
    reporte l'`url` obtenue dans le champ `signature_url` de `POST /visits`.

    Ce découplage évite de charger le payload de création de visite avec une image
    encodée en base64, et réutilise le même `StorageService` que les images MRZ —
    donc le même chemin de migration vers S3.
    """
    content = await signature.read()
    extension = validate_image_upload(signature.filename, content)
    url = await storage.save(content, folder="signatures", extension=extension)
    return UploadResponse(url=url)
