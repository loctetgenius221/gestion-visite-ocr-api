"""Paramètres système.

Ne sont exposés ici que des réglages **métier** (seuils, délais). Tout ce qui
touche à l'infrastructure ou aux secrets reste dans les variables
d'environnement : une valeur modifiable par une route HTTP n'est pas un secret.

Deux portées, selon le rôle de l'appelant :

- l'administrateur de plateforme lit et écrit les valeurs communes, et peut
  viser explicitement une organisation avec `?organization_id=` ;
- l'administrateur d'organisation lit les valeurs effectives de la sienne —
  défauts de la plateforme compris — et ne règle que ce qui relève de son
  exploitation : heure de clôture automatique, seuil de visite longue.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.core.deps import AdminScope, ClientContextDep, SessionDep, SettingServiceDep
from app.core.errors import InsufficientRoleError
from app.schemas.setting import SystemSettingsRead, SystemSettingsUpdate
from app.services.audit_service import AuditAction, AuditService

router = APIRouter(prefix="/settings", tags=["Administration — Paramètres"])


@router.get("", response_model=SystemSettingsRead, summary="Paramètres effectifs")
async def read_settings(
    service: SettingServiceDep,
    scope: AdminScope,
    organization_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Organisation dont on veut les valeurs effectives. Réservé à "
                "l'administrateur de plateforme ; les autres lisent la leur."
            )
        ),
    ] = None,
) -> SystemSettingsRead:
    """Défauts de l'application, surchargés par la plateforme puis par l'organisation.

    `organization_overrides` dit lesquels sont réellement surchargés : les autres
    suivent la valeur de plateforme et changeront avec elle.
    """
    return await service.get_settings(organization_id=scope.resolve(organization_id))


@router.put("", response_model=SystemSettingsRead, summary="Met à jour les paramètres")
async def update_settings(
    payload: SystemSettingsUpdate,
    service: SettingServiceDep,
    session: SessionDep,
    context: ClientContextDep,
    scope: AdminScope,
    organization_id: Annotated[
        uuid.UUID | None,
        Query(description="Organisation à régler. Réservé à l'administrateur de plateforme."),
    ] = None,
) -> SystemSettingsRead:
    """Mise à jour partielle : seuls les champs fournis sont modifiés.

    Un paramètre hors du périmètre de l'appelant est **refusé**, jamais ignoré :
    la réponse énumère alors les paramètres recevables.
    """
    cible = scope.resolve(organization_id)
    settings_effectifs, changements = await service.update_settings(
        payload, scope.user, organization_id=cible
    )

    if changements:
        # Journalisé après coup, dans sa propre transaction : la mise à jour est
        # déjà validée, et perdre la trace ne doit pas défaire le changement.
        audit = AuditService(session)
        await audit.record(
            AuditAction.SETTINGS_UPDATED,
            entity="settings",
            actor=scope.user,
            organization_id=cible,
            metadata={"changements": changements, "portee": settings_effectifs.scope},
            context=context,
        )
        await session.commit()

    return settings_effectifs


@router.delete(
    "/overrides/{key}",
    response_model=SystemSettingsRead,
    summary="Retire une surcharge d'organisation",
)
async def reset_override(
    service: SettingServiceDep,
    session: SessionDep,
    context: ClientContextDep,
    scope: AdminScope,
    key: Annotated[str, Path(max_length=100, description="Nom du paramètre à réaligner.")],
    organization_id: Annotated[
        uuid.UUID | None,
        Query(description="Organisation visée. Réservé à l'administrateur de plateforme."),
    ] = None,
) -> SystemSettingsRead:
    """Le paramètre suit à nouveau la valeur de plateforme, et changera avec elle.

    Distinct d'un `PUT` qui recopierait la valeur courante de la plateforme : cette
    dernière figerait la valeur du jour, et la structure cesserait de bénéficier des
    ajustements ultérieurs sans que personne ne s'en aperçoive.
    """
    cible = scope.resolve(organization_id)
    if cible is None:
        raise InsufficientRoleError(
            "Précisez l'organisation dont la surcharge doit être retirée.",
        )

    effectifs = await service.reset_override(key, scope.user, organization_id=cible)
    audit = AuditService(session)
    await audit.record(
        AuditAction.SETTINGS_UPDATED,
        entity="settings",
        actor=scope.user,
        organization_id=cible,
        metadata={"surcharge_retiree": key},
        context=context,
    )
    await session.commit()
    return effectifs
