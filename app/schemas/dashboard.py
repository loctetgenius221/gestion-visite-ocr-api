from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class DashboardStats(BaseModel):
    """Statistiques du poste d'accueil, alimente le `statCard` côté Flutter."""

    date: date
    visites_du_jour: int
    presents_actuellement: int
    sorties_du_jour: int
    visites_semaine: int
    duree_moyenne_visite_minutes: float | None = None
