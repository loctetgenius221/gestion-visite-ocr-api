from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Granularity = Literal["day", "week", "month"]


class DashboardStats(BaseModel):
    """Statistiques du poste d'accueil, alimente le `statCard` côté Flutter."""

    date: date
    visites_du_jour: int
    presents_actuellement: int
    sorties_du_jour: int
    visites_semaine: int
    duree_moyenne_visite_minutes: float | None = None


# --- Analytics du dashboard web (rôle ADMIN) ---------------------------------
#
# Les visites annulées sont exclues de tous les agrégats ci-dessous : une visite
# annulée est une erreur de saisie, la compter fausserait les statistiques.


class AnalyticsPeriod(BaseModel):
    """Fenêtre effectivement analysée, renvoyée avec chaque agrégat.

    Le client fournit des bornes optionnelles ; le serveur applique ses défauts.
    Renvoyer la période retenue évite au dashboard de les redeviner pour titrer
    ses graphiques.
    """

    date_from: datetime
    date_to: datetime


class TimeSeriesPoint(BaseModel):
    bucket: date = Field(
        description="Début de la tranche : le jour, le lundi de la semaine, ou le 1er du mois."
    )
    visites: int
    sorties: int


class TimeSeriesResponse(BaseModel):
    granularity: Granularity
    period: AnalyticsPeriod
    points: list[TimeSeriesPoint]


class BreakdownItem(BaseModel):
    id: uuid.UUID | None = Field(default=None, description="Nul pour l'agrégat « non renseigné ».")
    label: str
    visites: int
    pourcentage: float = Field(description="Part du total de la période, en pourcentage.")


class BreakdownResponse(BaseModel):
    period: AnalyticsPeriod
    total: int
    items: list[BreakdownItem]


class PeakHourItem(BaseModel):
    heure: int = Field(ge=0, le=23, description="Heure d'entrée, en UTC.")
    visites: int


class PeakHoursResponse(BaseModel):
    period: AnalyticsPeriod
    heures: list[PeakHourItem]


class AvgDurationResponse(BaseModel):
    period: AnalyticsPeriod
    visites_cloturees: int = Field(description="Visites effectivement sorties sur la période.")
    duree_moyenne_minutes: float | None = None
    duree_mediane_minutes: float | None = None
    # La médiane accompagne la moyenne : une visite oubliée en « présent » puis
    # clôturée le lendemain suffit à faire dérailler une moyenne seule.
    duree_max_minutes: float | None = None


class TopAgentItem(BaseModel):
    agent_id: uuid.UUID
    agent_name: str
    service_name: str | None = None
    visites: int


class TopAgentsResponse(BaseModel):
    period: AnalyticsPeriod
    items: list[TopAgentItem]
