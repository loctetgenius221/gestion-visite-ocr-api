from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.referentiel import Agent, Purpose, Service


class ServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Service]:
        stmt = select(Service).order_by(Service.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_id(self, service_id: uuid.UUID) -> Service | None:
        return await self.session.get(Service, service_id)

    async def exists(self, service_id: uuid.UUID) -> bool:
        stmt = select(Service.id).where(Service.id == service_id)
        return (await self.session.execute(stmt)).first() is not None


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, service_id: uuid.UUID | None = None) -> list[Agent]:
        stmt = select(Agent).order_by(Agent.name)
        if service_id is not None:
            stmt = stmt.where(Agent.service_id == service_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_id(self, agent_id: uuid.UUID) -> Agent | None:
        return await self.session.get(Agent, agent_id)


class PurposeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Purpose]:
        stmt = select(Purpose).order_by(Purpose.libelle)
        return list((await self.session.execute(stmt)).scalars().all())

    async def exists(self, purpose_id: uuid.UUID) -> bool:
        stmt = select(Purpose.id).where(Purpose.id == purpose_id)
        return (await self.session.execute(stmt)).first() is not None
