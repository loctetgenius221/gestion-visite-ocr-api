"""Contrat du périmètre d'organisation, vu depuis la couche service.

`app.core.deps` importe les services pour les câbler à FastAPI ; les services ne
peuvent donc pas importer `deps` en retour. Ce protocole donne à la couche métier
le type dont elle a besoin — `TenantScope` s'y conforme par construction — sans
créer de cycle, et sans que les services connaissent quoi que ce soit de FastAPI.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.models.user import User


@runtime_checkable
class TenantScopeLike(Protocol):
    """Périmètre d'organisation résolu, tel que la couche métier l'utilise.

    Les membres sont déclarés en **lecture seule** : un périmètre se résout une
    fois pour la requête et ne se réécrit pas en cours de route. `TenantScope` est
    d'ailleurs un dataclass figé, qu'un protocole à attributs mutables refuserait.
    """

    @property
    def user(self) -> User:
        """Compte à l'origine de la requête."""
        ...

    @property
    def organization_id(self) -> uuid.UUID | None:
        """Organisation de l'appelant. `None` = administrateur de plateforme."""
        ...

    @property
    def is_global(self) -> bool:
        """`True` pour l'administrateur de plateforme, seul à voir toutes les structures."""
        ...

    def ensure_can_access(self, organization_id: uuid.UUID | None) -> None:
        """Lève `CrossOrganizationError` si la ressource sort du périmètre."""
        ...

    def resolve(self, requested: uuid.UUID | None) -> uuid.UUID | None:
        """Organisation à filtrer ou à écrire, à partir de celle demandée."""
        ...
