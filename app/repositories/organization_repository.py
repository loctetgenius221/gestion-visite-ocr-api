from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole, VisitStatus
from app.models.organization import Organization
from app.models.referentiel import Agent, Service
from app.models.user import User
from app.models.visit import Visit
from app.schemas.organization import (
    OrganizationCreate,
    OrganizationFilters,
    OrganizationStats,
    OrganizationUpdate,
)

# Champs librement modifiables par `update`. `is_active` en est volontairement
# exclu : l'activation passe par `OrganizationService.set_status`, qui contrôle
# les visites en cours et journalise l'opération sous son propre verbe d'audit.
CHAMPS_TEXTE = ("nom", "localisation", "description", "contact_email", "contact_phone")
CHAMPS_BRANDING = ("primary_color", "secondary_color", "accent_color", "theme_config")


class OrganizationRepository:
    """Accès aux données des organisations / structures."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, organization_id: uuid.UUID) -> Organization | None:
        stmt = select(Organization).where(Organization.id == organization_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Organization | None:
        stmt = select(Organization).where(func.lower(Organization.code) == code.strip().lower())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _appliquer_filtres(stmt: Select, filters: OrganizationFilters | None) -> Select:
        """Filtres communs au comptage et à la liste.

        Factorisés : dupliqués, les deux jeux de conditions finissent par diverger
        et la pagination annonce un total qui ne correspond plus aux lignes servies.
        """
        if filters is None:
            return stmt
        if filters.is_active is not None:
            stmt = stmt.where(Organization.is_active.is_(filters.is_active))
        if filters.search:
            motif = f"%{filters.search.strip()}%"
            stmt = stmt.where(
                or_(
                    Organization.nom.ilike(motif),
                    Organization.code.ilike(motif),
                    Organization.localisation.ilike(motif),
                )
            )
        if filters.organization_id is not None:
            stmt = stmt.where(Organization.id == filters.organization_id)
        return stmt

    async def list_all(
        self,
        filters: OrganizationFilters | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Sequence[Organization]:
        stmt = self._appliquer_filtres(select(Organization), filters)
        trier_par_date = filters is not None and filters.sort_by == "created_at"
        colonne = Organization.created_at if trier_par_date else Organization.nom
        sens = colonne.desc() if filters and filters.sort == "desc" else colonne.asc()
        # `Organization.id` en second critère : deux structures homonymes créées
        # dans la même transaction rendraient sinon la pagination instable.
        stmt = stmt.order_by(sens, Organization.id).offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count(self, filters: OrganizationFilters | None = None) -> int:
        stmt = self._appliquer_filtres(select(func.count(Organization.id)), filters)
        result = await self._session.execute(stmt)
        return int(result.scalar_one() or 0)

    async def count_active_visits(self, organization_id: uuid.UUID) -> int:
        """Visites encore ouvertes — garde-fou de la désactivation."""
        stmt = select(func.count(Visit.id)).where(
            Visit.organization_id == organization_id,
            Visit.statut == VisitStatus.PRESENT,
        )
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    async def count_administrators(self, organization_id: uuid.UUID, *, actifs: bool = True) -> int:
        stmt = select(func.count(User.id)).where(
            User.organization_id == organization_id,
            User.role == UserRole.ORG_ADMIN,
        )
        if actifs:
            stmt = stmt.where(User.is_active.is_(True))
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    async def create(self, payload: OrganizationCreate) -> Organization:
        org = Organization(
            nom=payload.nom.strip(),
            code=payload.code.strip().upper(),
            localisation=payload.localisation.strip() if payload.localisation else None,
            description=payload.description.strip() if payload.description else None,
            contact_email=payload.contact_email.strip().lower() if payload.contact_email else None,
            contact_phone=payload.contact_phone.strip() if payload.contact_phone else None,
            primary_color=payload.primary_color,
            secondary_color=payload.secondary_color,
            accent_color=payload.accent_color,
            theme_config=payload.theme_config,
            is_active=True,
        )
        self._session.add(org)
        await self._session.flush()
        return org

    async def update(self, org: Organization, payload: OrganizationUpdate) -> Organization:
        """Mise à jour partielle : seuls les champs **présents** dans la requête bougent.

        `model_fields_set` et non `is not None` : la nuance décide si l'interface
        peut retirer une valeur. En testant la nullité, un `null` explicite — « je
        veux effacer la localisation », « je retire la couleur d'accent » — était
        confondu avec un champ absent, et aucun champ facultatif ne pouvait plus
        jamais être vidé une fois renseigné.
        """
        fournis = payload.model_fields_set

        if "code" in fournis and payload.code is not None:
            org.code = payload.code.strip().upper()

        for champ in CHAMPS_TEXTE:
            if champ not in fournis:
                continue
            valeur = getattr(payload, champ)
            if isinstance(valeur, str):
                valeur = valeur.strip() or None
                if valeur and champ == "contact_email":
                    valeur = valeur.lower()
            # `nom` est obligatoire : un `null` explicite ne doit pas le vider.
            if champ == "nom" and valeur is None:
                continue
            setattr(org, champ, valeur)

        for champ in CHAMPS_BRANDING:
            if champ in fournis:
                setattr(org, champ, getattr(payload, champ))

        await self._session.flush()
        return org

    async def set_logo(self, org: Organization, logo_url: str | None) -> Organization:
        """Réservé au service de logo : `logo_url` n'est pas modifiable en clair.

        Le champ porte une URL servie sous notre propre domaine ; laisser un
        client y écrire une chaîne arbitraire reviendrait à lui laisser placer une
        ressource tierce — donc un traceur — sur chaque écran du dashboard.
        """
        org.logo_url = logo_url
        await self._session.flush()
        return org

    async def set_active(self, org: Organization, is_active: bool) -> Organization:
        org.is_active = is_active
        await self._session.flush()
        return org

    async def get_stats(self, organization_id: uuid.UUID) -> OrganizationStats:
        services_count = (
            await self._session.execute(
                select(func.count(Service.id)).where(Service.organization_id == organization_id)
            )
        ).scalar_one() or 0

        agents_count = (
            await self._session.execute(
                select(func.count(Agent.id))
                .join(Service, Agent.service_id == Service.id)
                .where(Service.organization_id == organization_id)
            )
        ).scalar_one() or 0

        users_count = (
            await self._session.execute(
                select(func.count(User.id)).where(User.organization_id == organization_id)
            )
        ).scalar_one() or 0

        visits_count = (
            await self._session.execute(
                select(func.count(Visit.id)).where(Visit.organization_id == organization_id)
            )
        ).scalar_one() or 0

        active_visits_count = (
            await self._session.execute(
                select(func.count(Visit.id)).where(
                    Visit.organization_id == organization_id,
                    Visit.statut == "PRESENT",
                )
            )
        ).scalar_one() or 0

        return OrganizationStats(
            organization_id=organization_id,
            total_services=services_count,
            total_agents=agents_count,
            total_users=users_count,
            total_visits=visits_count,
            active_visits_count=active_visits_count,
        )
