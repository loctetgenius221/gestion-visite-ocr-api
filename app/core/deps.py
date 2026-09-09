from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.errors import (
    CrossOrganizationError,
    InsufficientRoleError,
    UnauthorizedError,
    UnscopedAccountError,
)
from app.core.logging import get_logger
from app.models.enums import UserRole
from app.models.user import User
from app.services.audit_service import ClientContext
from app.services.auth_service import AuthService
from app.services.auto_closure_service import AutoClosureService
from app.services.dashboard_service import DashboardService
from app.services.mrz_ocr_service import MrzOcrService
from app.services.organization_service import OrganizationService
from app.services.referentiel_admin_service import ReferentielAdminService
from app.services.referentiel_import_service import ReferentielImportService
from app.services.setting_service import SettingService
from app.services.storage_service import StorageService, get_storage_service
from app.services.user_admin_service import UserAdminService
from app.services.visit_service import VisitService

logger = get_logger(__name__)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/token", auto_error=False
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[StorageService, Depends(get_storage_service)]


def get_client_context(request: Request) -> ClientContext:
    """Origine de la requête, pour enrichir le journal d'audit."""
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


def get_referentiel_import_service(session: SessionDep) -> ReferentielImportService:
    return ReferentielImportService(session)


def get_setting_service(session: SessionDep) -> SettingService:
    return SettingService(session)


def get_organization_service(session: SessionDep, storage: StorageDep) -> OrganizationService:
    return OrganizationService(session, storage=storage)


def get_auto_closure_service(session: SessionDep) -> AutoClosureService:
    return AutoClosureService(session)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
VisitServiceDep = Annotated[VisitService, Depends(get_visit_service)]
DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
MrzOcrServiceDep = Annotated[MrzOcrService, Depends(get_mrz_ocr_service)]
UserAdminServiceDep = Annotated[UserAdminService, Depends(get_user_admin_service)]
ReferentielAdminServiceDep = Annotated[
    ReferentielAdminService, Depends(get_referentiel_admin_service)
]
ReferentielImportServiceDep = Annotated[
    ReferentielImportService, Depends(get_referentiel_import_service)
]
SettingServiceDep = Annotated[SettingService, Depends(get_setting_service)]
OrganizationServiceDep = Annotated[OrganizationService, Depends(get_organization_service)]
AutoClosureServiceDep = Annotated[AutoClosureService, Depends(get_auto_closure_service)]


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
    """Garde des routes d'administration globale ou d'organisation."""
    if current_user.role not in (UserRole.ADMIN, UserRole.ORG_ADMIN):
        logger.warning(
            "Accès administrateur refusé",
            extra={"identifiant": current_user.identifiant, "role": current_user.role.value},
        )
        raise InsufficientRoleError(details={"role_requis": UserRole.ADMIN.value})
    return current_user


AdminUser = Annotated[User, Depends(get_current_admin)]


async def get_current_super_admin(current_user: CurrentUser) -> User:
    """Garde réservé à l'administrateur global de la plateforme."""
    if current_user.role is not UserRole.ADMIN:
        logger.warning(
            "Accès super administrateur refusé",
            extra={"identifiant": current_user.identifiant, "role": current_user.role.value},
        )
        raise InsufficientRoleError(details={"role_requis": UserRole.ADMIN.value})
    return current_user


SuperAdminUser = Annotated[User, Depends(get_current_super_admin)]


async def get_current_supervisor_or_admin(current_user: CurrentUser) -> User:
    if current_user.role not in (UserRole.ADMIN, UserRole.ORG_ADMIN, UserRole.SUPERVISEUR):
        raise InsufficientRoleError(
            details={
                "roles_requis": [
                    UserRole.SUPERVISEUR.value,
                    UserRole.ORG_ADMIN.value,
                    UserRole.ADMIN.value,
                ]
            }
        )
    return current_user


SupervisorOrAdminUser = Annotated[User, Depends(get_current_supervisor_or_admin)]


# --- Périmètre d'organisation ------------------------------------------------


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Périmètre de données d'un appelant, résolu une fois pour la requête.

    `organization_id is None` signifie **accès global**, et c'est le seul
    privilège propre à l'ADMIN de plateforme. Tout autre rôle est rattaché à une
    organisation ; un compte non-ADMIN sans rattachement est refusé à la porte
    (`UnscopedAccountError`) plutôt que laissé traverser l'application.

    Cette distinction n'est pas cosmétique : le cloisonnement était auparavant
    réécrit à la main dans chaque router, avec trois formulations divergentes
    (`organization_id is not None` ici, une liste de rôles là), et les routes
    oubliées — recherche de visiteurs, listes de services et d'agents — laissaient
    fuiter les données des autres structures. Un périmètre unique, injecté, retire
    au router la possibilité de se tromper.
    """

    user: User
    organization_id: uuid.UUID | None

    @property
    def is_global(self) -> bool:
        return self.organization_id is None

    def ensure_can_access(self, organization_id: uuid.UUID | None) -> None:
        """Vérifie qu'une ressource déjà chargée est bien dans le périmètre."""
        if self.is_global:
            return
        if organization_id != self.organization_id:
            raise CrossOrganizationError()

    def resolve(self, requested: uuid.UUID | None) -> uuid.UUID | None:
        """Organisation à filtrer ou à écrire, à partir de celle demandée.

        Pour un appelant global, la demande fait foi (`None` = toutes). Pour tout
        autre, la sienne s'impose, et demander celle d'un tiers est refusé au lieu
        d'être silencieusement réécrit — un filtre ignoré sans le dire donnerait à
        l'appelant l'illusion d'avoir consulté ce qu'il demandait.
        """
        if self.is_global:
            return requested
        if requested is not None and requested != self.organization_id:
            raise CrossOrganizationError()
        return self.organization_id


async def get_tenant_scope(current_user: CurrentUser) -> TenantScope:
    if current_user.role is UserRole.ADMIN:
        return TenantScope(user=current_user, organization_id=None)
    if current_user.organization_id is None:
        logger.warning(
            "Compte sans organisation de rattachement",
            extra={"identifiant": current_user.identifiant, "role": current_user.role.value},
        )
        raise UnscopedAccountError()
    return TenantScope(user=current_user, organization_id=current_user.organization_id)


Scope = Annotated[TenantScope, Depends(get_tenant_scope)]


async def get_admin_scope(admin: AdminUser, scope: Scope) -> TenantScope:
    """Périmètre d'un appelant dont le rôle d'administration est déjà vérifié.

    `admin` n'est pas utilisé dans le corps : il est déclaré pour sa **garde**.
    FastAPI résout la dépendance, qui refuse tout rôle autre qu'`ADMIN` ou
    `ORG_ADMIN` avant que la route ne s'exécute. Combiner les deux ici évite à
    chaque route d'administration de déclarer deux paramètres dont l'un ne lui
    sert jamais — et d'en oublier un.
    """
    return scope


AdminScope = Annotated[TenantScope, Depends(get_admin_scope)]


ORGANIZATION_FILTER_DESCRIPTION = (
    "Restreint la réponse à une organisation. Seul l'ADMIN de plateforme a de quoi "
    "en changer : tout autre compte est déjà borné à la sienne, et demander celle "
    "d'un tiers est refusé (`CROSS_ORGANIZATION`) plutôt qu'ignoré."
)


async def get_organization_filter(
    scope: Scope,
    organization_id: Annotated[
        uuid.UUID | None, Query(description=ORGANIZATION_FILTER_DESCRIPTION)
    ] = None,
) -> uuid.UUID | None:
    """Organisation à filtrer sur une route de lecture, `None` pour « toutes ».

    Déclarer le paramètre ici plutôt que route par route lui donne partout le même
    nom, la même description et surtout la même résolution : c'est `TenantScope`
    qui tranche, jamais le router. Une route qui l'oublie ne filtre plus du tout,
    ce qui se voit ; une route qui le réimplémente filtrerait de travers, ce qui
    ne se voit pas.
    """
    return scope.resolve(organization_id)


OrganizationFilter = Annotated[uuid.UUID | None, Depends(get_organization_filter)]
