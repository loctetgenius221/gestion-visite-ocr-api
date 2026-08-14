from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, File, Query, UploadFile, status

from app.core.deps import CurrentUser, StorageDep
from app.schemas.upload import UploadResponse
from app.services.storage_service import validate_image_upload

router = APIRouter(prefix="/uploads", tags=["Fichiers"])

DocumentFace = Literal["recto", "verso"]


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


@router.post(
    "/document",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dépose la photo d'une face de la pièce d'identité",
)
async def upload_document(
    storage: StorageDep,
    current_user: CurrentUser,
    document: Annotated[UploadFile, File(description="Photo d'une face de la pièce.")],
    face: DocumentFace = Query(description="Face photographiée : `recto` ou `verso`."),
) -> UploadResponse:
    """Reporte l'`url` obtenue dans `document_recto_url` ou `document_verso_url`.

    Le scan MRZ (`POST /ocr/scan`) dépose déjà la face qu'il a lue — le verso d'une
    CNI. Cette route couvre le cas où l'OCR échoue et où l'agent saisit l'identité à
    la main : le **recto** porte alors la photo et des mentions absentes du verso,
    et c'est la seule pièce justificative de ce qui a été saisi (ADR-018).

    Les deux faces sont rangées séparément (`documents/recto/`, `documents/verso/`) :
    la purge des images périmées et toute inspection ultérieure y gagnent, pour un
    coût nul.
    """
    content = await document.read()
    extension = validate_image_upload(document.filename, content)
    url = await storage.save(content, folder=f"documents/{face}", extension=extension)
    return UploadResponse(url=url)
