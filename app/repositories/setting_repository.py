from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SystemSetting


class SettingRepository:
    """Paramètres système, en clé/valeur : ajouter un paramètre ne coûte pas de migration."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all_values(self) -> dict[str, Any]:
        """Écarts au défaut, sous forme `{clé: valeur}`.

        La valeur est déballée de son enveloppe `{"value": ...}` : les scalaires
        JSON à la racine ne sont pas portables entre PostgreSQL et SQLite.
        """
        rows = (await self.session.execute(select(SystemSetting))).scalars().all()
        return {row.key: (row.value or {}).get("value") for row in rows}

    async def last_updated_at(self) -> datetime | None:
        stmt = select(func.max(SystemSetting.updated_at))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(self, key: str, value: Any, updated_by: uuid.UUID | None = None) -> None:
        stmt = select(SystemSetting).where(SystemSetting.key == key)
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        if record is None:
            self.session.add(SystemSetting(key=key, value={"value": value}, updated_by=updated_by))
        else:
            record.value = {"value": value}
            record.updated_by = updated_by
        await self.session.flush()
