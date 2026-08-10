"""Paramètres système — réservé au rôle ADMIN.

Ne sont exposés ici que des réglages **métier** (seuils, délais). Tout ce qui
touche à l'infrastructure ou aux secrets reste dans les variables
d'environnement : une valeur modifiable par une route HTTP n'est pas un secret.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import AdminUser, ClientContextDep, SessionDep, SettingServiceDep
from app.schemas.setting import SystemSettingsRead, SystemSettingsUpdate
from app.services.audit_service import AuditAction, AuditService

router = APIRouter(prefix="/settings", tags=["Administration — Paramètres"])


@router.get("", response_model=SystemSettingsRead, summary="Paramètres effectifs")
async def read_settings(
    service: SettingServiceDep, current_admin: AdminUser
) -> SystemSettingsRead:
    """Valeurs par défaut de l'application, surchargées par celles enregistrées."""
    return await service.get_settings()


@router.put("", response_model=SystemSettingsRead, summary="Met à jour les paramètres")
async def update_settings(
    payload: SystemSettingsUpdate,
    service: SettingServiceDep,
    session: SessionDep,
    context: ClientContextDep,
    current_admin: AdminUser,
) -> SystemSettingsRead:
    """Mise à jour partielle : seuls les champs fournis sont modifiés."""
    settings_effectifs, changements = await service.update_settings(payload, current_admin)

    if changements:
        # Journalisé après coup, dans sa propre transaction : la mise à jour est
        # déjà validée, et perdre la trace ne doit pas défaire le changement.
        audit = AuditService(session)
        await audit.record(
            AuditAction.SETTINGS_UPDATED,
            entity="settings",
            actor=current_admin,
            metadata={"changements": changements},
            context=context,
        )
        await session.commit()

    return settings_effectifs
