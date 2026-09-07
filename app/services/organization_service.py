"""Gestion des organisations : cycle de vie, administrateurs, charte graphique.

Le périmètre d'accès n'est jamais recalculé ici : il arrive résolu, sous forme de
`TenantScope`, et le service se contente de l'appliquer. C'est ce qui permet aux
routes de ne plus porter de logique de cloisonnement — et donc de ne plus l'oublier.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps_types import TenantScopeLike
from app.core.errors import (
    DuplicateIdentifiantError,
    DuplicateOrganizationCodeError,
    InsufficientRoleError,
    OrganizationHasActiveVisitsError,
    OrganizationNotFoundError,
)
from app.core.logging import get_logger
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.organization import Organization
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common import Page, PaginationParams
from app.schemas.organization import (
    OrganizationAdminCreate,
    OrganizationCreate,
    OrganizationFilters,
    OrganizationRead,
    OrganizationStats,
    OrganizationStatusUpdate,
    OrganizationUpdate,
    OrganizationWithAdminResponse,
)
from app.schemas.user import UserAdminRead
from app.services.audit_service import AuditAction, AuditService, diff
from app.services.storage_service import StorageService, normalize_logo
from app.services.user_admin_service import generate_password

logger = get_logger(__name__)

# Champs repris dans le diff d'audit d'une modification. `logo_url` en fait
# partie bien qu'il ne soit pas modifiable par `update` : il change par la route
# de dépôt, et l'audit doit le montrer.
CHAMPS_AUDITES = (
    "nom",
    "code",
    "localisation",
    "description",
    "contact_email",
    "contact_phone",
    "is_active",
    "logo_url",
    "primary_color",
    "secondary_color",
    "accent_color",
    "theme_config",
)


class OrganizationService:
    """Gestion métier des organisations et structures."""

    def __init__(self, session: AsyncSession, storage: StorageService | None = None) -> None:
        self._session = session
        self._storage = storage
        self._org_repo = OrganizationRepository(session)
        self._user_repo = UserRepository(session)
        self._audit = AuditService(session)

    # --- Lecture -------------------------------------------------------------

    async def list_organizations(
        self,
        filters: OrganizationFilters | None,
        pagination: PaginationParams | None,
        scope: TenantScopeLike,
    ) -> Page[OrganizationRead]:
        """Liste paginée. Un appelant non global n'y voit que son organisation.

        Le cloisonnement passe par le filtre plutôt que par un court-circuit :
        la pagination, le tri et le total restent alors ceux de la requête
        normale, au lieu d'une page bricolée qui annonçait `total=1` quelle que
        soit la page demandée.
        """
        p = pagination or PaginationParams()
        filters = (filters or OrganizationFilters()).model_copy(
            update={"organization_id": scope.organization_id}
        )
        total = await self._org_repo.count(filters)
        items = await self._org_repo.list_all(filters=filters, offset=p.offset, limit=p.page_size)
        return Page(
            items=[OrganizationRead.model_validate(item) for item in items],
            total=total,
            page=p.page,
            page_size=p.page_size,
        )

    async def get_organization(
        self, organization_id: uuid.UUID, scope: TenantScopeLike | None = None
    ) -> OrganizationRead:
        org = await self._require(organization_id, scope)
        return OrganizationRead.model_validate(org)

    async def get_stats(
        self, organization_id: uuid.UUID, scope: TenantScopeLike | None = None
    ) -> OrganizationStats:
        await self._require(organization_id, scope)
        return await self._org_repo.get_stats(organization_id)

    async def list_administrators(
        self, organization_id: uuid.UUID, scope: TenantScopeLike | None = None
    ) -> list[UserAdminRead]:
        """Comptes `ORG_ADMIN` de la structure, actifs ou non.

        Sert le garde-fou côté interface : avant de désactiver un administrateur,
        le dashboard doit pouvoir montrer qui d'autre peut prendre le relais.
        """
        await self._require(organization_id, scope)
        comptes = await self._user_repo.list_by_organization_and_roles(
            organization_id, roles=(UserRole.ORG_ADMIN,)
        )
        return [UserAdminRead.model_validate(compte) for compte in comptes]

    # --- Écriture ------------------------------------------------------------

    async def create_organization(
        self, payload: OrganizationCreate, actor: User
    ) -> OrganizationWithAdminResponse:
        existing = await self._org_repo.get_by_code(payload.code)
        if existing:
            raise DuplicateOrganizationCodeError(
                f"Une organisation porte déjà le code « {payload.code.strip().upper()} »."
            )

        # Tout est validé **avant** la première écriture : organisation et premier
        # administrateur naissent ensemble ou pas du tout. Vérifier l'identifiant
        # après avoir inséré la structure ferait dépendre l'atomicité du rollback
        # de la session, et laisserait, s'il venait à manquer, une organisation
        # sans personne pour l'administrer — visible dans la liste, impossible à
        # prendre en main.
        admin_demande: OrganizationAdminCreate | None = None
        if payload.admin_identifiant and payload.admin_nom:
            admin_demande = OrganizationAdminCreate(
                nom=payload.admin_nom,
                identifiant=payload.admin_identifiant,
                mot_de_passe=payload.admin_mot_de_passe,
            )
            identifiant = admin_demande.identifiant.strip().lower()
            if await self._user_repo.get_by_identifiant(identifiant) is not None:
                raise DuplicateIdentifiantError(details={"identifiant": identifiant})

        try:
            org = await self._org_repo.create(payload)
        except IntegrityError as err:
            # Course entre deux créations du même code : la contrainte d'unicité
            # tranche, on la traduit dans notre format d'erreur.
            await self._session.rollback()
            logger.warning(
                "Conflit de création d'organisation",
                extra={"code": payload.code, "erreur": str(err)},
            )
            raise DuplicateOrganizationCodeError() from err

        admin_read: UserAdminRead | None = None
        mot_de_passe_temporaire: str | None = None

        if admin_demande is not None:
            admin, mot_de_passe_temporaire = await self._creer_administrateur(
                org, admin_demande, actor
            )
            admin_read = UserAdminRead.model_validate(admin)

        await self._audit.record(
            AuditAction.ORGANIZATION_CREATED,
            entity="organization",
            entity_id=org.id,
            actor=actor,
            organization_id=org.id,
            metadata={
                "code": org.code,
                "nom": org.nom,
                "administrateur_cree": admin_read.identifiant if admin_read else None,
            },
        )
        await self._session.commit()
        return OrganizationWithAdminResponse(
            organization=OrganizationRead.model_validate(org),
            admin_user=admin_read,
            admin_temporary_password=mot_de_passe_temporaire,
        )

    async def update_organization(
        self,
        organization_id: uuid.UUID,
        payload: OrganizationUpdate,
        scope: TenantScopeLike,
    ) -> OrganizationRead:
        org = await self._require(organization_id, scope)

        if payload.code is not None:
            autre = await self._org_repo.get_by_code(payload.code)
            if autre is not None and autre.id != organization_id:
                raise DuplicateOrganizationCodeError(
                    f"Une organisation porte déjà le code « {payload.code.strip().upper()} »."
                )

        avant = self._instantane(org)
        org = await self._org_repo.update(org, payload)
        changements = diff(avant, self._instantane(org))

        if changements:
            await self._audit.record(
                AuditAction.ORGANIZATION_UPDATED,
                entity="organization",
                entity_id=org.id,
                actor=scope.user,
                organization_id=org.id,
                metadata={"changements": changements},
            )
        await self._session.commit()
        return OrganizationRead.model_validate(org)

    async def set_status(
        self,
        organization_id: uuid.UUID,
        payload: OrganizationStatusUpdate,
        scope: TenantScopeLike,
    ) -> OrganizationRead:
        """Active ou désactive une organisation.

        Désactiver coupe l'accès de **tous** ses comptes dès la requête suivante,
        sessions ouvertes comprises (`AuthService` le vérifie à chaque résolution
        de token). C'est pour cela que l'opération est refusée tant que des
        visiteurs sont présents : les agents du poste ne pourraient plus
        enregistrer leur sortie, et le registre resterait ouvert sur des visites
        qu'aucune route ne saurait plus clôturer.
        """
        org = await self._require(organization_id, scope)

        if org.is_active == payload.is_active:
            return OrganizationRead.model_validate(org)

        if not payload.is_active:
            en_cours = await self._org_repo.count_active_visits(organization_id)
            if en_cours:
                raise OrganizationHasActiveVisitsError(
                    details={"visites_en_cours": en_cours, "organization_id": str(organization_id)}
                )

        org = await self._org_repo.set_active(org, payload.is_active)
        await self._audit.record(
            AuditAction.ORGANIZATION_STATUS_CHANGED,
            entity="organization",
            entity_id=org.id,
            actor=scope.user,
            organization_id=org.id,
            metadata={"is_active": payload.is_active, "motif": payload.motif},
        )
        await self._session.commit()
        logger.info(
            "Organisation %s",
            "activée" if payload.is_active else "désactivée",
            extra={"organisation": org.code, "acteur": scope.user.identifiant},
        )
        return OrganizationRead.model_validate(org)

    async def add_administrator(
        self,
        organization_id: uuid.UUID,
        payload: OrganizationAdminCreate,
        scope: TenantScopeLike,
    ) -> tuple[UserAdminRead, str | None]:
        """Nomme un administrateur sur une organisation existante.

        Retourne `(compte, mot de passe en clair ou None)` : le mot de passe n'est
        renvoyé que s'il a été **généré** par le serveur, et c'est l'unique
        occasion de le lire.
        """
        org = await self._require(organization_id, scope)
        admin, mot_de_passe = await self._creer_administrateur(org, payload, scope.user)

        await self._audit.record(
            AuditAction.ORGANIZATION_ADMIN_ADDED,
            entity="organization",
            entity_id=org.id,
            actor=scope.user,
            organization_id=org.id,
            metadata={"identifiant": admin.identifiant, "user_id": str(admin.id)},
        )
        await self._session.commit()
        return UserAdminRead.model_validate(admin), mot_de_passe

    # --- Charte graphique ----------------------------------------------------

    async def set_logo(
        self,
        organization_id: uuid.UUID,
        content: bytes,
        scope: TenantScopeLike,
    ) -> OrganizationRead:
        """Dépose le logo : validé par son contenu, réencodé, puis stocké.

        L'ancien fichier est supprimé après le remplacement, jamais avant : si le
        dépôt échoue, l'organisation garde le logo qu'elle avait plutôt que de se
        retrouver sans.
        """
        org = await self._require(organization_id, scope)
        storage = self._require_storage()

        png = normalize_logo(content)
        ancien = org.logo_url
        # Un nom neuf à chaque dépôt : réécrire le même chemin laisserait les
        # caches — navigateur, proxy — servir l'ancien logo pendant des heures.
        url = await storage.save(
            png, folder=f"organizations/{organization_id}", extension=".png"
        )

        org = await self._org_repo.set_logo(org, url)
        await self._audit.record(
            AuditAction.ORGANIZATION_LOGO_UPDATED,
            entity="organization",
            entity_id=org.id,
            actor=scope.user,
            organization_id=org.id,
            metadata={"logo_url": url, "octets": len(png)},
        )
        await self._session.commit()

        if ancien:
            await storage.delete(ancien)
        return OrganizationRead.model_validate(org)

    async def remove_logo(
        self, organization_id: uuid.UUID, scope: TenantScopeLike
    ) -> OrganizationRead:
        org = await self._require(organization_id, scope)
        ancien = org.logo_url
        if ancien is None:
            return OrganizationRead.model_validate(org)

        org = await self._org_repo.set_logo(org, None)
        await self._audit.record(
            AuditAction.ORGANIZATION_LOGO_REMOVED,
            entity="organization",
            entity_id=org.id,
            actor=scope.user,
            organization_id=org.id,
            metadata={"logo_url": ancien},
        )
        await self._session.commit()

        await self._require_storage().delete(ancien)
        return OrganizationRead.model_validate(org)

    async def get_theme(self, code: str) -> OrganizationRead:
        """Charte d'une organisation **active**, par son code, sans authentification.

        Sert la page de connexion, qui doit porter les couleurs de la structure
        avant que quiconque ait pu s'identifier. Une organisation désactivée est
        traitée comme inexistante : rien n'y est plus accessible, sa devanture
        pas davantage.
        """
        org = await self._org_repo.get_by_code(code)
        if org is None or not org.is_active:
            raise OrganizationNotFoundError(details={"code": code})
        return OrganizationRead.model_validate(org)

    # --- Aides ---------------------------------------------------------------

    async def _require(
        self, organization_id: uuid.UUID, scope: TenantScopeLike | None
    ) -> Organization:
        """Charge l'organisation et vérifie qu'elle est dans le périmètre de l'appelant.

        L'ordre compte : le contrôle de périmètre vient **après** le chargement,
        pour qu'une organisation inexistante réponde 404 et non 403 — l'inverse
        confirmerait l'existence d'une structure à qui n'y a pas accès.
        """
        org = await self._org_repo.get_by_id(organization_id)
        if org is None:
            raise OrganizationNotFoundError(details={"organization_id": str(organization_id)})
        if scope is not None:
            scope.ensure_can_access(org.id)
        return org

    def _require_storage(self) -> StorageService:
        if self._storage is None:  # pragma: no cover - garde de programmation
            raise InsufficientRoleError("Stockage de fichiers indisponible.")
        return self._storage

    @staticmethod
    def _instantane(org: Organization) -> dict[str, object]:
        return {champ: getattr(org, champ) for champ in CHAMPS_AUDITES}

    async def _creer_administrateur(
        self, org: Organization, payload: OrganizationAdminCreate, actor: User
    ) -> tuple[User, str | None]:
        identifiant = payload.identifiant.strip().lower()
        if await self._user_repo.get_by_identifiant(identifiant) is not None:
            raise DuplicateIdentifiantError(details={"identifiant": identifiant})

        genere = payload.mot_de_passe is None
        mot_de_passe = payload.mot_de_passe or generate_password()

        admin = User(
            nom=payload.nom.strip(),
            identifiant=identifiant,
            poste=payload.poste,
            mot_de_passe_hash=hash_password(mot_de_passe),
            role=UserRole.ORG_ADMIN,
            organization_id=org.id,
            is_active=True,
        )
        try:
            await self._user_repo.add(admin)
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateIdentifiantError(details={"identifiant": identifiant}) from exc

        await self._audit.record(
            AuditAction.USER_CREATED,
            entity="user",
            entity_id=admin.id,
            actor=actor,
            organization_id=org.id,
            metadata={"identifiant": admin.identifiant, "role": admin.role.value},
        )
        return admin, (mot_de_passe if genere else None)
