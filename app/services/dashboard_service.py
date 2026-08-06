from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.visit_repository import VisitRepository
from app.schemas.dashboard import DashboardStats


class DashboardService:
    """Agrégats du poste d'accueil pour l'écran d'accueil Flutter."""

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
