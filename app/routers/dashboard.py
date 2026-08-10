"""Statistiques.

`/dashboard/stats` reste ouvert à tout compte authentifié — l'app mobile s'en sert
pour son écran d'accueil. Les analytics sous `/dashboard/stats/*` sont réservés au
rôle ADMIN : elles portent sur l'activité de tous les postes.

Les visites annulées sont exclues de tous les agrégats.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import AdminUser, CurrentUser, DashboardServiceDep
from app.schemas.dashboard import (
    AvgDurationResponse,
    BreakdownResponse,
    DashboardStats,
    Granularity,
    PeakHoursResponse,
    TimeSeriesResponse,
    TopAgentsResponse,
)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


class PeriodQuery:
    """Bornes de période, communes à toutes les analytics.

    Les deux bornes sont optionnelles : à défaut, le service applique une fenêtre
    de 30 jours et **renvoie la période retenue** dans sa réponse, pour que le
    dashboard puisse titrer ses graphiques sans la redeviner.
    """

    def __init__(
        self,
        date_from: datetime | None = Query(default=None, description="Début de la période."),
        date_to: datetime | None = Query(default=None, description="Fin de la période."),
    ) -> None:
        self.date_from = date_from
        self.date_to = date_to


PeriodDep = Annotated[PeriodQuery, Depends()]


@router.get("/stats", response_model=DashboardStats, summary="Statistiques du poste d'accueil")
async def get_stats(service: DashboardServiceDep, current_user: CurrentUser) -> DashboardStats:
    return await service.get_stats()


@router.get(
    "/stats/timeseries",
    response_model=TimeSeriesResponse,
    summary="Série temporelle des entrées et sorties",
)
async def get_timeseries(
    service: DashboardServiceDep,
    current_admin: AdminUser,
    period: PeriodDep,
    granularity: Granularity = Query(default="day"),
) -> TimeSeriesResponse:
    """Toutes les tranches de la période sont présentes, y compris celles à zéro :
    un graphe qui saute les jours creux laisse croire à une continuité inexistante."""
    return await service.timeseries(granularity, period.date_from, period.date_to)


@router.get(
    "/stats/by-service",
    response_model=BreakdownResponse,
    summary="Répartition des visites par service",
)
async def get_by_service(
    service: DashboardServiceDep, current_admin: AdminUser, period: PeriodDep
) -> BreakdownResponse:
    return await service.by_service(period.date_from, period.date_to)


@router.get(
    "/stats/by-purpose",
    response_model=BreakdownResponse,
    summary="Répartition des visites par motif",
)
async def get_by_purpose(
    service: DashboardServiceDep, current_admin: AdminUser, period: PeriodDep
) -> BreakdownResponse:
    """Les visites à motif libre sont regroupées sous une entrée `id: null`."""
    return await service.by_purpose(period.date_from, period.date_to)


@router.get(
    "/stats/peak-hours",
    response_model=PeakHoursResponse,
    summary="Histogramme des entrées par heure",
)
async def get_peak_hours(
    service: DashboardServiceDep, current_admin: AdminUser, period: PeriodDep
) -> PeakHoursResponse:
    """Les 24 heures sont toujours renvoyées, à zéro le cas échéant. Heures en UTC."""
    return await service.peak_hours(period.date_from, period.date_to)


@router.get(
    "/stats/avg-duration",
    response_model=AvgDurationResponse,
    summary="Durée moyenne de présence",
)
async def get_avg_duration(
    service: DashboardServiceDep, current_admin: AdminUser, period: PeriodDep
) -> AvgDurationResponse:
    """La médiane accompagne la moyenne : une visite oubliée en « présent » puis
    clôturée le lendemain suffit à faire dérailler une moyenne seule."""
    return await service.avg_duration(period.date_from, period.date_to)


@router.get(
    "/stats/top-agents",
    response_model=TopAgentsResponse,
    summary="Personnes les plus visitées",
)
async def get_top_agents(
    service: DashboardServiceDep,
    current_admin: AdminUser,
    period: PeriodDep,
    limit: int = Query(default=10, ge=1, le=100),
) -> TopAgentsResponse:
    return await service.top_agents(limit, period.date_from, period.date_to)
