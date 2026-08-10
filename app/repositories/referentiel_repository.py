from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.referentiel import Agent, Purpose, Service

# Les listes servies à l'app mobile ne montrent que l'actif : un service archivé
# ne doit plus être proposé à l'agent de contrôle. Le dashboard, lui, demande
# explicitement `include_archived=True` pour administrer l'existant.


class ServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, *, include_archived: bool = False) -> list[Service]:
        stmt = select(Service).order_by(Service.name)
        if not include_archived:
            stmt = stmt.where(Service.is_archived.is_(False))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_id(self, service_id: uuid.UUID) -> Service | None:
        return await self.session.get(Service, service_id)

    async def get_by_code(self, code: str) -> Service | None:
        stmt = select(Service).where(func.lower(Service.code) == code.strip().lower())
        return (await self.session.execute(stmt)).scalars().first()

    async def exists(self, service_id: uuid.UUID) -> bool:
        stmt = select(Service.id).where(Service.id == service_id)
        return (await self.session.execute(stmt)).first() is not None

    async def add(self, service: Service) -> Service:
        self.session.add(service)
        await self.session.flush()
        return service

    async def count_agents(self, service_id: uuid.UUID, *, only_active: bool = True) -> int:
        stmt = select(func.count(Agent.id)).where(Agent.service_id == service_id)
        if only_active:
            stmt = stmt.where(Agent.is_archived.is_(False))
        return (await self.session.execute(stmt)).scalar_one()

    async def count_children(self, service_id: uuid.UUID, *, only_active: bool = True) -> int:
        stmt = select(func.count(Service.id)).where(Service.parent_id == service_id)
        if only_active:
            stmt = stmt.where(Service.is_archived.is_(False))
        return (await self.session.execute(stmt)).scalar_one()

    async def descendant_ids(self, service_id: uuid.UUID) -> set[uuid.UUID]:
        """Identifiants du sous-arbre, service inclus.

        Sert à refuser qu'un service devienne son propre ancêtre : la hiérarchie
        est parcourue par `_build_tree` à chaque listing, et un cycle y bouclerait
        indéfiniment. Le parcours se fait en mémoire — l'arbre des directions d'un
        ministère compte quelques dizaines de nœuds, pas de quoi justifier un CTE
        récursif non portable vers SQLite.
        """
        services = list((await self.session.execute(select(Service))).scalars().all())
        enfants: dict[uuid.UUID, list[uuid.UUID]] = {}
        for service in services:
            if service.parent_id is not None:
                enfants.setdefault(service.parent_id, []).append(service.id)

        vus: set[uuid.UUID] = set()
        a_visiter = [service_id]
        while a_visiter:
            courant = a_visiter.pop()
            if courant in vus:
                continue
            vus.add(courant)
            a_visiter.extend(enfants.get(courant, []))
        return vus


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(
        self, service_id: uuid.UUID | None = None, *, include_archived: bool = False
    ) -> list[Agent]:
        stmt = select(Agent).order_by(Agent.name)
        if service_id is not None:
            stmt = stmt.where(Agent.service_id == service_id)
        if not include_archived:
            stmt = stmt.where(Agent.is_archived.is_(False))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_id(self, agent_id: uuid.UUID) -> Agent | None:
        return await self.session.get(Agent, agent_id)

    async def get_by_name_in_service(self, name: str, service_id: uuid.UUID) -> Agent | None:
        stmt = select(Agent).where(
            func.lower(Agent.name) == name.strip().lower(), Agent.service_id == service_id
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def add(self, agent: Agent) -> Agent:
        self.session.add(agent)
        await self.session.flush()
        return agent


class PurposeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, *, include_archived: bool = False) -> list[Purpose]:
        stmt = select(Purpose).order_by(Purpose.libelle)
        if not include_archived:
            stmt = stmt.where(Purpose.is_archived.is_(False))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_id(self, purpose_id: uuid.UUID) -> Purpose | None:
        return await self.session.get(Purpose, purpose_id)

    async def get_by_libelle(self, libelle: str) -> Purpose | None:
        stmt = select(Purpose).where(func.lower(Purpose.libelle) == libelle.strip().lower())
        return (await self.session.execute(stmt)).scalars().first()

    async def exists(self, purpose_id: uuid.UUID) -> bool:
        stmt = select(Purpose.id).where(Purpose.id == purpose_id)
        return (await self.session.execute(stmt)).first() is not None

    async def add(self, purpose: Purpose) -> Purpose:
        self.session.add(purpose)
        await self.session.flush()
        return purpose
