from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.errors import InsufficientRoleError, UnauthorizedError
from app.core.logging import get_logger
from app.models.enums import UserRole
from app.models.user import User
from app.services.audit_service import ClientContext
from app.services.auth_service import AuthService
from app.services.dashboard_service import DashboardService
from app.services.mrz_ocr_service import MrzOcrService
from app.services.referentiel_admin_service import ReferentielAdminService
from app.services.setting_service import SettingService
from app.services.storage_service import StorageService, get_storage_service
from app.services.user_admin_service import UserAdminService
from app.services.visit_service import VisitService

logger = get_logger(__name__)

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


def get_client_context(request: Request) -> ClientContext:
    """Origine de la requête, pour enrichir le journal d'audit.

    L'IP vient de `request.client` — qu'uvicorn renseigne depuis `X-Forwarded-For`
    grâce à `--proxy-headers`. Sans ce réglage côté serveur, toutes les entrées
    du journal porteraient l'adresse du reverse proxy.
    """
    return ClientContext(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


ClientContextDep = Annotated[ClientContext, Depends(get_client_context)]


def get_auth_service(session: SessionDep, context: ClientContextDep) -> AuthService:
    return AuthService(session, context)


def get_visit_service(session: SessionDep, context: ClientContextDep) -> VisitService:
    return VisitService(session, context)


def get_dashboard_service(session: SessionDep) -> DashboardService:
    return DashboardService(session)


def get_mrz_ocr_service(storage: StorageDep) -> MrzOcrService:
    return MrzOcrService(storage=storage)


def get_user_admin_service(session: SessionDep) -> UserAdminService:
    return UserAdminService(session)


def get_referentiel_admin_service(session: SessionDep) -> ReferentielAdminService:
    return ReferentielAdminService(session)


def get_setting_service(session: SessionDep) -> SettingService:
    return SettingService(session)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
VisitServiceDep = Annotated[VisitService, Depends(get_visit_service)]
DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
MrzOcrServiceDep = Annotated[MrzOcrService, Depends(get_mrz_ocr_service)]
UserAdminServiceDep = Annotated[UserAdminService, Depends(get_user_admin_service)]
ReferentielAdminServiceDep = Annotated[
    ReferentielAdminService, Depends(get_referentiel_admin_service)
]
SettingServiceDep = Annotated[SettingService, Depends(get_setting_service)]


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    auth_service: AuthServiceDep,
) -> User:
    """Résout l'utilisateur authentifié à partir du bearer token."""
    if not token:
        raise UnauthorizedError("Token d'authentification manquant.")
    return await auth_service.resolve_access_token(token)


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_admin(current_user: CurrentUser) -> User:
    """Garde des routes d'administration.

    Le rôle est relu **en base** à chaque requête, via `get_current_user` : jamais
    depuis la revendication `role` du JWT. Un jeton émis avant une rétrogradation
    porte encore l'ancien rôle, et une revendication reste une donnée fournie par
    le client — la contrôler côté serveur est le seul modèle défendable.
    """
    if current_user.role is not UserRole.ADMIN:
        logger.warning(
            "Accès administrateur refusé",
            extra={"identifiant": current_user.identifiant, "role": current_user.role.value},
        )
        raise InsufficientRoleError(details={"role_requis": UserRole.ADMIN.value})
    return current_user


AdminUser = Annotated[User, Depends(get_current_admin)]
