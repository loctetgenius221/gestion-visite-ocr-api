"""Paramètres système modifiables par un administrateur.

Les valeurs par défaut sont portées par le schéma `SystemSettingsRead` ; la table
ne stocke que les écarts. Conséquence utile : une base vide donne exactement le
comportement documenté, et un paramètre retiré du schéma cesse simplement d'être
lu, sans migration ni ligne orpheline gênante.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.user import User
from app.repositories.setting_repository import SettingRepository
from app.schemas.setting import SystemSettingsRead, SystemSettingsUpdate

logger = get_logger(__name__)


class SettingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = SettingRepository(session)

    async def get_settings(self) -> SystemSettingsRead:
        """Paramètres effectifs : défauts du schéma, surchargés par la base."""
        overrides = await self.settings.all_values()
        connus = set(SystemSettingsRead.model_fields)
        # Une clé inconnue en base — paramètre retiré depuis — est ignorée plutôt
        # que de faire échouer la lecture par une erreur de validation.
        retenus = {key: value for key, value in overrides.items() if key in connus}
        return SystemSettingsRead(
            **retenus, updated_at=await self.settings.last_updated_at()
        )

    async def update_settings(
        self, payload: SystemSettingsUpdate, actor: User
    ) -> tuple[SystemSettingsRead, dict[str, object]]:
        """Applique une mise à jour partielle. Retourne l'état final et les changements.

        Les changements sont renvoyés à l'appelant pour qu'il les journalise :
        c'est le router qui possède le contexte de la requête.
        """
        avant = await self.get_settings()
        modifications = payload.model_dump(exclude_unset=True, exclude_none=True)

        changements: dict[str, object] = {}
        for key, value in modifications.items():
            ancienne = getattr(avant, key)
            if ancienne != value:
                await self.settings.upsert(key, value, updated_by=actor.id)
                changements[key] = {"avant": ancienne, "après": value}

        await self.session.commit()
        return await self.get_settings(), changements
