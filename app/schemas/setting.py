"""Schémas des paramètres système.

Les valeurs par défaut vivent ici, dans le schéma de lecture : la table
`system_settings` ne stocke que les écarts au défaut. Ajouter un paramètre se
réduit donc à ajouter un champ ici — aucune migration.

Deux portées, séparées par `CLES_ORGANISATION` :

- **plateforme** — sécurité et conservation légale (verrouillage des comptes,
  rétention des images, garde-fou d'export). Réservées à l'administrateur global :
  ce sont des règles que toutes les structures subissent ensemble ;
- **organisation** — exploitation du poste d'accueil (heure de clôture, seuil de
  visite longue). Chaque structure y règle son propre fonctionnement, sans
  toucher à celui des autres.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Réglages qu'une organisation peut surcharger pour elle-même. Tout ce qui n'y
# figure pas reste une décision de plateforme.
CLES_ORGANISATION: frozenset[str] = frozenset(
    {
        "visit_long_duration_alert_minutes",
        "automatic_visit_closure_enabled",
        "automatic_visit_closure_time",
    }
)


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
    referentiel_import_max_rows: int = Field(
        default=5_000,
        ge=10,
        le=50_000,
        description=(
            "Garde-fou symétrique, à l'import des référentiels : nombre de lignes "
            "au-delà duquel un classeur est refusé. Plus bas que le plafond "
            "d'export, parce qu'un import s'applique en une seule transaction — "
            "il écrit, là où l'export ne fait que lire."
        ),
    )

    document_images_retention_days: int = Field(
        default=365,
        ge=0,
        le=3650,
        description=(
            "Durée de conservation des photos de pièces d'identité, comptée depuis "
            "la dernière visite du visiteur. `0` désactive la purge. Les visites "
            "restent au registre : seules les images sont supprimées. Appliqué par "
            "`python -m app.purge_documents`, jamais automatiquement."
        ),
    )

    automatic_visit_closure_enabled: bool = Field(
        default=True,
        description=(
            "Active ou désactive la clôture automatique quotidienne des visites "
            "restées PRESENT."
        ),
    )
    automatic_visit_closure_time: str = Field(
        default="17:00",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Heure quotidienne de clôture automatique au format HH:MM (ex: 17:00).",
    )

    updated_at: datetime | None = Field(
        default=None,
        description="Dernière modification d'un paramètre, tous paramètres confondus.",
    )
    organization_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Organisation dont les surcharges sont appliquées ci-dessus. "
            "`null` = valeurs de plateforme."
        ),
    )
    scope: Literal["platform", "organization"] = Field(
        default="platform",
        description="Portée effective de la lecture.",
    )
    organization_overrides: list[str] = Field(
        default_factory=list,
        description=(
            "Paramètres effectivement surchargés par l'organisation. Les autres "
            "suivent la valeur de plateforme et changeront avec elle."
        ),
    )


class SystemSettingsUpdate(BaseModel):
    """Mise à jour partielle : seuls les champs fournis sont modifiés.

    Un administrateur d'organisation ne peut fournir que les clés de
    `CLES_ORGANISATION` ; toute autre est refusée explicitement plutôt
    qu'ignorée en silence — croire avoir durci le verrouillage des comptes de
    toute la plateforme alors qu'il ne s'est rien passé serait pire que l'erreur.
    """

    visit_long_duration_alert_minutes: int | None = Field(default=None, ge=5, le=1440)
    max_failed_login_attempts: int | None = Field(default=None, ge=3, le=20)
    account_lockout_minutes: int | None = Field(default=None, ge=1, le=1440)
    visits_export_max_rows: int | None = Field(default=None, ge=100, le=500_000)
    referentiel_import_max_rows: int | None = Field(default=None, ge=10, le=50_000)
    document_images_retention_days: int | None = Field(default=None, ge=0, le=3650)
    automatic_visit_closure_enabled: bool | None = None
    automatic_visit_closure_time: str | None = Field(
        default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
