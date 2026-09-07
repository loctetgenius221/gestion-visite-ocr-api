"""Paramètres système modifiables par un administrateur.

Les valeurs par défaut sont portées par le schéma `SystemSettingsRead` ; la table
ne stocke que les écarts. Conséquence utile : une base vide donne exactement le
comportement documenté, et un paramètre retiré du schéma cesse simplement d'être
lu, sans migration ni ligne orpheline gênante.

La lecture se fait en cascade — défaut du schéma, puis valeur de plateforme, puis
surcharge de l'organisation. L'écriture, elle, dépend du rôle : l'administrateur
de plateforme fixe les valeurs communes, celui d'une organisation ne règle que
l'exploitation de la sienne (`CLES_ORGANISATION`).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientRoleError
from app.core.logging import get_logger
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.setting_repository import SettingRepository
from app.schemas.setting import CLES_ORGANISATION, SystemSettingsRead, SystemSettingsUpdate

logger = get_logger(__name__)


class SettingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = SettingRepository(session)

    async def get_settings(
        self, *, organization_id: uuid.UUID | None = None
    ) -> SystemSettingsRead:
        """Paramètres effectifs pour une organisation, ou de plateforme sans elle."""
        connus = set(SystemSettingsRead.model_fields)
        # Une clé inconnue en base — paramètre retiré depuis — est ignorée plutôt
        # que de faire échouer la lecture par une erreur de validation.
        valeurs = {
            key: value
            for key, value in (await self.settings.all_values()).items()
            if key in connus
        }

        surcharges: dict[str, object] = {}
        if organization_id is not None:
            propres = await self.settings.all_values(organization_id=organization_id)
            # Une clé sortie de `CLES_ORGANISATION` depuis son enregistrement ne
            # doit plus s'appliquer : la portée fait foi à la lecture, pas la ligne.
            surcharges = {
                key: value
                for key, value in propres.items()
                if key in connus and key in CLES_ORGANISATION
            }
            valeurs.update(surcharges)

        return SystemSettingsRead(
            **valeurs,
            updated_at=await self.settings.last_updated_at(organization_id=organization_id),
            organization_id=organization_id,
            scope="organization" if organization_id is not None else "platform",
            organization_overrides=sorted(surcharges),
        )

    async def update_settings(
        self,
        payload: SystemSettingsUpdate,
        actor: User,
        *,
        organization_id: uuid.UUID | None = None,
    ) -> tuple[SystemSettingsRead, dict[str, object]]:
        """Applique une mise à jour partielle. Retourne l'état final et les changements.

        Les changements sont renvoyés à l'appelant pour qu'il les journalise :
        c'est le router qui possède le contexte de la requête.
        """
        modifications = payload.model_dump(exclude_unset=True, exclude_none=True)
        cible = self._portee_ecriture(actor, organization_id, modifications)

        avant = await self.get_settings(organization_id=cible)
        changements: dict[str, object] = {}
        for key, value in modifications.items():
            ancienne = getattr(avant, key)
            if ancienne != value:
                await self.settings.upsert(
                    key, value, organization_id=cible, updated_by=actor.id
                )
                changements[key] = {"avant": ancienne, "après": value}

        await self.session.commit()
        return await self.get_settings(organization_id=cible), changements

    async def reset_override(
        self, key: str, actor: User, *, organization_id: uuid.UUID
    ) -> SystemSettingsRead:
        """Retire une surcharge : le paramètre suit à nouveau la valeur de plateforme."""
        if key not in CLES_ORGANISATION:
            raise InsufficientRoleError(
                f"Le paramètre « {key} » n'est pas réglable par une organisation.",
                details={"cles_autorisees": sorted(CLES_ORGANISATION)},
            )
        await self.settings.delete(key, organization_id=organization_id)
        await self.session.commit()
        return await self.get_settings(organization_id=organization_id)

    @staticmethod
    def _portee_ecriture(
        actor: User,
        organization_id: uuid.UUID | None,
        modifications: dict[str, object],
    ) -> uuid.UUID | None:
        """Portée dans laquelle écrire, et refus des clés hors du périmètre de l'acteur.

        L'administrateur de plateforme écrit les valeurs communes ; les clés
        hors périmètre d'un administrateur d'organisation sont refusées, jamais
        ignorées — croire avoir durci le verrouillage des comptes de toute la
        plateforme alors que rien n'a bougé serait pire que l'erreur elle-même.
        """
        if actor.role is UserRole.ADMIN and organization_id is None:
            return None

        hors_perimetre = sorted(set(modifications) - CLES_ORGANISATION)
        if hors_perimetre:
            raise InsufficientRoleError(
                "Ces paramètres relèvent de la plateforme et ne peuvent pas être "
                "réglés organisation par organisation.",
                details={
                    "parametres_refuses": hors_perimetre,
                    "parametres_autorises": sorted(CLES_ORGANISATION),
                },
            )
        return organization_id
