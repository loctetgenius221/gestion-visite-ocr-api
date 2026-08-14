"""Recherche des visiteurs déjà connus.

Une personne qui revient au poste d'accueil n'a pas à faire rescanner sa pièce :
l'agent la retrouve ici, puis enregistre la visite avec `visitor_id` (ADR-017).

La route n'expose que la lecture. Aucune écriture directe sur une fiche visiteur
n'est ouverte : l'identité vient du scan MRZ, et la corriger relève d'un autre
geste métier, avec sa propre traçabilité.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, VisitServiceDep
from app.schemas.common import Page, PaginationParams
from app.schemas.visit import VisitorSearchResult

router = APIRouter(prefix="/visitors", tags=["Visiteurs"])


def get_pagination(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


@router.get(
    "",
    response_model=Page[VisitorSearchResult],
    summary="Recherche un visiteur déjà enregistré",
)
async def search_visitors(
    service: VisitServiceDep,
    current_user: CurrentUser,
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
    search: str = Query(
        min_length=3,
        max_length=100,
        description="Nom, prénom, numéro de document ou NIN. Trois caractères minimum.",
    ),
) -> Page[VisitorSearchResult]:
    """Fiches triées de la venue la plus récente à la plus ancienne.

    Les trois caractères minimum ne sont pas une commodité : sans eux, une seule
    lettre suffirait à parcourir le fichier des visiteurs, pièces d'identité
    comprises.

    Chaque fiche porte `visite_ouverte_id` quand la personne est **encore présente**.
    Enregistrer une nouvelle visite dans ce cas est refusé (`VISITOR_ALREADY_PRESENT`) :
    le champ permet de proposer la clôture directement, sans passer par l'erreur.
    """
    return await service.search_visitors(search, pagination, current_user)
