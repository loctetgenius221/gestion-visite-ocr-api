from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import SystemSetting


class SettingRepository:
    """Paramètres système, en clé/valeur : ajouter un paramètre ne coûte pas de migration.

    Deux portées cohabitent dans la table, distinguées par `organization_id` :
    `NULL` pour la plateforme, renseigné pour la surcharge d'une organisation.
    Toutes les méthodes prennent donc la portée en argument, jamais par défaut —
    un oubli lirait ou écrirait la valeur de tout le monde.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all_values(self, *, organization_id: uuid.UUID | None = None) -> dict[str, Any]:
        """Écarts au défaut d'une portée, sous forme `{clé: valeur}`.

        La valeur est déballée de son enveloppe `{"value": ...}` : les scalaires
        JSON à la racine ne sont pas portables entre PostgreSQL et SQLite.
        """
        stmt = select(SystemSetting).where(
            SystemSetting.organization_id == organization_id
            if organization_id is not None
            else SystemSetting.organization_id.is_(None)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.key: (row.value or {}).get("value") for row in rows}

    async def last_updated_at(
        self, *, organization_id: uuid.UUID | None = None
    ) -> datetime | None:
        """Dernière modification touchant la portée lue, surcharges comprises.

        Les deux portées sont considérées quand on lit une organisation : ses
        valeurs effectives bougent aussi quand la plateforme change un défaut
        qu'elle n'a pas surchargé.
        """
        stmt = select(func.max(SystemSetting.updated_at))
        if organization_id is not None:
            stmt = stmt.where(
                SystemSetting.organization_id.in_([organization_id])
                | SystemSetting.organization_id.is_(None)
            )
        else:
            stmt = stmt.where(SystemSetting.organization_id.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        key: str,
        value: Any,
        *,
        organization_id: uuid.UUID | None = None,
        updated_by: uuid.UUID | None = None,
    ) -> None:
        stmt = select(SystemSetting).where(
            SystemSetting.key == key,
            SystemSetting.organization_id == organization_id
            if organization_id is not None
            else SystemSetting.organization_id.is_(None),
        )
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        if record is None:
            self.session.add(
                SystemSetting(
                    key=key,
                    value={"value": value},
                    organization_id=organization_id,
                    updated_by=updated_by,
                )
            )
        else:
            record.value = {"value": value}
            record.updated_by = updated_by
        await self.session.flush()

    async def delete(self, key: str, *, organization_id: uuid.UUID) -> bool:
        """Retire la surcharge d'une organisation : le paramètre suit à nouveau la plateforme.

        Réservé aux surcharges : supprimer une valeur de plateforme n'aurait pas
        de sens distinct de la remettre à son défaut, et le défaut vit dans le
        schéma, pas en base.
        """
        stmt = select(SystemSetting).where(
            SystemSetting.key == key,
            SystemSetting.organization_id == organization_id,
        )
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        if record is None:
            return False
        await self.session.delete(record)
        await self.session.flush()
        return True
