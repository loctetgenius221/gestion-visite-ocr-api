from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.visit_repository import VisitRepository
from app.schemas.dashboard import (
    AnalyticsPeriod,
    AvgDurationResponse,
    BreakdownItem,
    BreakdownResponse,
    DashboardStats,
    Granularity,
    PeakHourItem,
    PeakHoursResponse,
    TimeSeriesPoint,
    TimeSeriesResponse,
    TopAgentItem,
    TopAgentsResponse,
)

# Fenêtre retenue quand le client ne borne pas sa demande : assez large pour être
# parlante, assez courte pour rester rapide sans index dédié.
DEFAULT_ANALYTICS_DAYS = 30
MOTIF_NON_RENSEIGNE = "Motif libre / non renseigné"


class DashboardService:
    """Agrégats du poste d'accueil (mobile) et analytics du dashboard web."""

    def __init__(self, session: AsyncSession) -> None:
        self.visits = VisitRepository(session)

    async def get_stats(self, now: datetime | None = None) -> DashboardStats:
        current = now or datetime.now(UTC)
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        # Semaine glissante sur 7 jours : plus parlant qu'un découpage lundi-dimanche
        # pour un poste d'accueil ouvert en continu.
        week_start = day_end - timedelta(days=7)

        durations = await self.visits.closed_visits_durations_seconds(day_start, day_end)
        moyenne = round(sum(durations) / len(durations) / 60, 1) if durations else None

        return DashboardStats(
            date=day_start.date(),
            visites_du_jour=await self.visits.count_checked_in_between(day_start, day_end),
            presents_actuellement=await self.visits.count_present(),
            sorties_du_jour=await self.visits.count_checked_out_between(day_start, day_end),
            visites_semaine=await self.visits.count_checked_in_between(week_start, day_end),
            duree_moyenne_visite_minutes=moyenne,
        )

    # --- Analytics -----------------------------------------------------------

    @staticmethod
    def resolve_period(
        date_from: datetime | None, date_to: datetime | None, now: datetime | None = None
    ) -> AnalyticsPeriod:
        """Complète les bornes manquantes et garantit `date_from <= date_to`.

        Des bornes inversées ne sont pas une erreur du client à sanctionner : on
        les remet à l'endroit, et la réponse porte la période réellement analysée.
        """
        current = now or datetime.now(UTC)
        fin = date_to or current
        debut = date_from or (fin - timedelta(days=DEFAULT_ANALYTICS_DAYS))
        if debut > fin:
            debut, fin = fin, debut
        return AnalyticsPeriod(date_from=debut, date_to=fin)

    @staticmethod
    def _bucket(moment: datetime, granularity: Granularity) -> date:
        """Ramène un horodatage au début de sa tranche."""
        jour = moment.date()
        if granularity == "day":
            return jour
        if granularity == "week":
            # Lundi de la semaine ISO.
            return jour - timedelta(days=jour.weekday())
        return jour.replace(day=1)

    @classmethod
    def _tranches(
        cls, period: AnalyticsPeriod, granularity: Granularity
    ) -> list[date]:
        """Toutes les tranches de la période, y compris celles sans aucune visite.

        Un graphe qui saute les jours creux laisse croire à une continuité qui
        n'existe pas : deux points espacés d'une semaine s'y affichent côte à côte.
        """
        tranches: list[date] = []
        courant = cls._bucket(period.date_from, granularity)
        fin = cls._bucket(period.date_to, granularity)
        while courant <= fin:
            tranches.append(courant)
            if granularity == "day":
                courant += timedelta(days=1)
            elif granularity == "week":
                courant += timedelta(days=7)
            else:
                courant = (courant.replace(day=28) + timedelta(days=4)).replace(day=1)
        return tranches

    async def timeseries(
        self,
        granularity: Granularity,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        now: datetime | None = None,
    ) -> TimeSeriesResponse:
        period = self.resolve_period(date_from, date_to, now)

        entrees = await self.visits.checkin_checkout_timestamps(period.date_from, period.date_to)
        sorties = await self.visits.checkout_timestamps(period.date_from, period.date_to)

        compte_entrees = Counter(self._bucket(inn, granularity) for inn, _ in entrees)
        compte_sorties = Counter(self._bucket(out, granularity) for out in sorties)

        return TimeSeriesResponse(
            granularity=granularity,
            period=period,
            points=[
                TimeSeriesPoint(
                    bucket=tranche,
                    visites=compte_entrees.get(tranche, 0),
                    sorties=compte_sorties.get(tranche, 0),
                )
                for tranche in self._tranches(period, granularity)
            ],
        )

    async def by_service(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        now: datetime | None = None,
    ) -> BreakdownResponse:
        period = self.resolve_period(date_from, date_to, now)
        lignes = await self.visits.count_by_service(period.date_from, period.date_to)
        return self._breakdown(period, [(sid, name, total) for sid, name, total in lignes])

    async def by_purpose(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        now: datetime | None = None,
    ) -> BreakdownResponse:
        period = self.resolve_period(date_from, date_to, now)
        lignes = await self.visits.count_by_purpose(period.date_from, period.date_to)
        return self._breakdown(
            period,
            # `purpose_id` est nullable : les visites à motif libre sont regroupées
            # sous un libellé explicite plutôt que d'être écartées du camembert.
            [(pid, libelle or MOTIF_NON_RENSEIGNE, total) for pid, libelle, total in lignes],
        )

    @staticmethod
    def _breakdown(
        period: AnalyticsPeriod, lignes: list[tuple[object, str, int]]
    ) -> BreakdownResponse:
        total = sum(nombre for _, _, nombre in lignes)
        return BreakdownResponse(
            period=period,
            total=total,
            items=[
                BreakdownItem(
                    id=identifiant,  # type: ignore[arg-type]
                    label=libelle,
                    visites=nombre,
                    # Le garde sur `total` évite la division par zéro d'une période vide.
                    pourcentage=round(nombre * 100 / total, 1) if total else 0.0,
                )
                for identifiant, libelle, nombre in lignes
            ],
        )

    async def peak_hours(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        now: datetime | None = None,
    ) -> PeakHoursResponse:
        period = self.resolve_period(date_from, date_to, now)
        entrees = await self.visits.checkin_checkout_timestamps(period.date_from, period.date_to)
        compte = Counter(inn.hour for inn, _ in entrees)
        # Les 24 heures sont toutes présentes, à zéro s'il le faut : un histogramme
        # à trous se lit mal, et le client n'a pas à compléter les manquantes.
        return PeakHoursResponse(
            period=period,
            heures=[PeakHourItem(heure=h, visites=compte.get(h, 0)) for h in range(24)],
        )

    async def avg_duration(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        now: datetime | None = None,
    ) -> AvgDurationResponse:
        period = self.resolve_period(date_from, date_to, now)
        secondes = await self.visits.closed_visits_durations_seconds(
            period.date_from, period.date_to
        )
        if not secondes:
            return AvgDurationResponse(period=period, visites_cloturees=0)

        minutes = [valeur / 60 for valeur in secondes]
        return AvgDurationResponse(
            period=period,
            visites_cloturees=len(minutes),
            duree_moyenne_minutes=round(statistics.fmean(minutes), 1),
            # La médiane accompagne la moyenne : une visite oubliée en « présent »
            # puis clôturée le lendemain suffit à faire dérailler une moyenne seule.
            duree_mediane_minutes=round(statistics.median(minutes), 1),
            duree_max_minutes=round(max(minutes), 1),
        )

    async def top_agents(
        self,
        limit: int = 10,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        now: datetime | None = None,
    ) -> TopAgentsResponse:
        period = self.resolve_period(date_from, date_to, now)
        lignes = await self.visits.top_agents(period.date_from, period.date_to, limit=limit)
        return TopAgentsResponse(
            period=period,
            items=[
                TopAgentItem(
                    agent_id=agent_id,
                    agent_name=nom,
                    service_name=service,
                    visites=nombre,
                )
                for agent_id, nom, service, nombre in lignes
            ],
        )
