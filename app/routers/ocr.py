from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.core.deps import CurrentUser, MrzOcrServiceDep
from app.schemas.ocr import MrzScanResponse

router = APIRouter(prefix="/ocr", tags=["OCR MRZ"])


@router.post(
    "/scan",
    response_model=MrzScanResponse,
    summary="Extrait les champs d'identité de la zone MRZ d'un document",
)
async def scan_mrz(
    service: MrzOcrServiceDep,
    current_user: CurrentUser,
    mrz_image: Annotated[UploadFile, File(description="Face du document portant le MRZ.")],
) -> MrzScanResponse:
    """Un seul fichier est attendu : la face portant le MRZ (verso de CNI, page
    d'identité du passeport). Aucune capture recto séparée n'est traitée.

    Un échec de checksum ICAO ne bloque pas le flux : la réponse porte alors
    `mrz_valid=false` et le détail par champ dans `checksum_details`, afin que
    l'agent de contrôle corrige manuellement côté mobile.
    """
    content = await mrz_image.read()
    return await service.scan(content, mrz_image.filename)
