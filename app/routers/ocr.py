"""Lecture MRZ côté serveur — solution de secours.

Le scan se fait sur le mobile (Google ML Kit). PaddleOCR ne sert que si cette
lecture cesse de fonctionner : il est désactivé par défaut, et l'administrateur
de plateforme l'active par le paramètre `server_ocr_enabled`, sans redéploiement.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.core.deps import CurrentUser, MrzOcrServiceDep, SettingServiceDep
from app.core.errors import ServerOcrDisabledError
from app.core.routing import StrictQueryParamsRoute
from app.schemas.ocr import MrzScanResponse, ServerOcrStatus

router = APIRouter(prefix="/ocr", tags=["OCR MRZ"], route_class=StrictQueryParamsRoute)


@router.get(
    "/status",
    response_model=ServerOcrStatus,
    summary="Disponibilité de la lecture MRZ serveur",
)
async def ocr_status(
    settings_service: SettingServiceDep, current_user: CurrentUser
) -> ServerOcrStatus:
    """Ouvert à tout compte authentifié : c'est l'app mobile qui en a besoin."""
    parametres = await settings_service.get_settings()
    return ServerOcrStatus(enabled=parametres.server_ocr_enabled)


@router.post(
    "/scan",
    response_model=MrzScanResponse,
    summary="Extrait les champs d'identité de la zone MRZ d'un document",
    responses={503: {"description": "Lecture MRZ serveur désactivée (`SERVER_OCR_DISABLED`)"}},
)
async def scan_mrz(
    service: MrzOcrServiceDep,
    settings_service: SettingServiceDep,
    current_user: CurrentUser,
    mrz_image: Annotated[UploadFile, File(description="Face du document portant le MRZ.")],
) -> MrzScanResponse:
    """Un seul fichier est attendu : la face portant le MRZ (verso de CNI, page
    d'identité du passeport). Aucune capture recto séparée n'est traitée.

    Un échec de checksum ICAO ne bloque pas le flux : la réponse porte alors
    `mrz_valid=false` et le détail par champ dans `checksum_details`, afin que
    l'agent de contrôle corrige manuellement côté mobile.

    Répond `503 SERVER_OCR_DISABLED` tant que l'OCR serveur n'est pas activé.
    Le contrôle est fait dans le corps de la route, après l'authentification :
    un appelant anonyme reçoit un 401, pas l'état d'un réglage de plateforme.
    """
    if not (await settings_service.get_settings()).server_ocr_enabled:
        raise ServerOcrDisabledError()
    content = await mrz_image.read()
    return await service.scan(content, mrz_image.filename)
