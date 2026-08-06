from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentUser, DashboardServiceDep
from app.schemas.dashboard import DashboardStats

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats", response_model=DashboardStats, summary="Statistiques du poste d'accueil")
async def get_stats(service: DashboardServiceDep, current_user: CurrentUser) -> DashboardStats:
    return await service.get_stats()
