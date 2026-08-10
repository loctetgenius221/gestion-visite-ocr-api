"""Schémas des paramètres système (rôle ADMIN).

Les valeurs par défaut vivent ici, dans le schéma de lecture : la table
`system_settings` ne stocke que les écarts au défaut. Ajouter un paramètre se
réduit donc à ajouter un champ ici — aucune migration.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SystemSettingsRead(BaseModel):
    """Paramètres effectifs : valeurs par défaut, surchargées par la base."""

    visit_long_duration_alert_minutes: int = Field(
        default=120,
        ge=5,
        le=1440,
        description=(
            "Au-delà de cette durée de présence, une visite est signalée comme "
            "anormalement longue sur le dashboard."
        ),
    )
    max_failed_login_attempts: int = Field(
        default=5,
        ge=3,
        le=20,
        description="Nombre d'échecs consécutifs avant verrouillage d'un compte.",
    )
    account_lockout_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="Durée du verrouillage. Un administrateur peut débloquer avant son terme.",
    )
    visits_export_max_rows: int = Field(
        default=50_000,
        ge=100,
        le=500_000,
        description="Garde-fou : nombre de lignes au-delà duquel un export est refusé.",
    )

    updated_at: datetime | None = Field(
        default=None, description="Dernière modification d'un paramètre, tous paramètres confondus."
    )


class SystemSettingsUpdate(BaseModel):
    """Mise à jour partielle : seuls les champs fournis sont modifiés."""

    visit_long_duration_alert_minutes: int | None = Field(default=None, ge=5, le=1440)
    max_failed_login_attempts: int | None = Field(default=None, ge=3, le=20)
    account_lockout_minutes: int | None = Field(default=None, ge=1, le=1440)
    visits_export_max_rows: int | None = Field(default=None, ge=100, le=500_000)
