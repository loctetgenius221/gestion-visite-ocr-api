from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.errors import UnauthorizedError
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.dashboard_service import DashboardService
from app.services.mrz_ocr_service import MrzOcrService
from app.services.storage_service import StorageService, get_storage_service
from app.services.visit_service import VisitService

# `tokenUrl` doit désigner un endpoint acceptant réellement le formulaire OAuth2
# (`username`/`password`) : c'est `/auth/token`, et non `/auth/login` qui attend du
# JSON. Sans quoi le bouton « Authorize » de Swagger enverrait un corps que l'API
# rejetterait en 400.
#
# `auto_error=False` : on gère nous-mêmes l'absence de token pour renvoyer le
# format d'erreur standard de la spec §5.6 plutôt que le détail FastAPI par défaut.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/token", auto_error=False
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[StorageService, Depends(get_storage_service)]


def get_auth_service(session: SessionDep) -> AuthService:
    return AuthService(session)


def get_visit_service(session: SessionDep) -> VisitService:
    return VisitService(session)


def get_dashboard_service(session: SessionDep) -> DashboardService:
    return DashboardService(session)


def get_mrz_ocr_service(storage: StorageDep) -> MrzOcrService:
    return MrzOcrService(storage=storage)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
VisitServiceDep = Annotated[VisitService, Depends(get_visit_service)]
DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
MrzOcrServiceDep = Annotated[MrzOcrService, Depends(get_mrz_ocr_service)]


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    auth_service: AuthServiceDep,
) -> User:
    """Résout l'utilisateur authentifié à partir du bearer token."""
    if not token:
        raise UnauthorizedError("Token d'authentification manquant.")
    return await auth_service.resolve_access_token(token)


CurrentUser = Annotated[User, Depends(get_current_user)]
