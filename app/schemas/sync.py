"""Données servies à l'app mobile pour travailler hors-ligne (ADR-025)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.organization import OrganizationThemeRead
from app.schemas.referentiel import AgentRead, PurposeRead, ServiceRead
from app.schemas.visit import VisitRead


class ParametresHorsLigne(BaseModel):
    """Réglages dont la tablette a besoin sans réseau."""

    automatic_visit_closure_enabled: bool
    automatic_visit_closure_time: str = Field(description="`HH:MM`, heure UTC.")
    visit_long_duration_alert_minutes: int
    server_ocr_enabled: bool


class ReferentielsHorsLigne(BaseModel):
    """Instantané complet des référentiels actifs d'une organisation.

    Un instantané plutôt qu'un delta : quelques centaines de lignes au plus, et
    aucune suppression à propager — l'archivage fait simplement sortir une ligne
    de la liste. Le client remplace son cache en bloc, dans une transaction.
    """

    version: str = Field(
        description=(
            "Empreinte du contenu, reprise dans l'en-tête `ETag`. Renvoyée dans "
            "`If-None-Match`, elle vaut une réponse `304` sans corps tant que rien "
            "n'a changé."
        )
    )
    server_time: datetime
    organization: OrganizationThemeRead
    services: list[ServiceRead] = Field(
        description="Liste à plat ; `parent_id` permet de reconstruire la hiérarchie."
    )
    agents: list[AgentRead]
    purposes: list[PurposeRead] = Field(description="Motifs propres et motifs communs.")
    parametres: ParametresHorsLigne

    @staticmethod
    def empreinte(contenu: dict) -> str:
        brut = json.dumps(contenu, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(brut.encode()).hexdigest()[:32]


class PresentsHorsLigne(BaseModel):
    """Visites encore ouvertes, pour enregistrer leur sortie sans réseau."""

    server_time: datetime
    total: int = Field(description="Nombre réel de visites ouvertes.")
    tronque: bool = Field(
        description="`true` si `items` ne contient pas tout : au-delà du plafond, "
        "les plus anciennes seulement."
    )
    items: list[VisitRead]
