"""Schémas de l'import en masse des référentiels (services, agents).

Le parcours se fait en deux temps, et le même schéma décrit les deux :

1. `POST …/import/preview` reçoit le classeur et rend le **plan** — ce qui serait
   créé, mis à jour, laissé tel quel, et ce qui bloque ;
2. `POST …/import` reçoit les lignes en JSON et rend le **résultat**.

Les deux réponses ont la même forme, parce que l'import est tout ou rien : s'il
s'applique, il fait exactement ce que le plan annonçait. Seul `applique` les
distingue. Le dashboard peut donc afficher une seule grille, la laisser corriger,
et la renvoyer.

Ce détour par du JSON — plutôt qu'un second envoi du fichier — n'est pas
gratuit : il rend l'étape d'application déterministe (le fichier ne peut plus
changer entre la prévisualisation et la validation) et permet à l'administrateur
de corriger une ligne dans la grille au lieu de rouvrir Excel.

Les champs de ligne ne portent **aucune contrainte de longueur** : toute la
validation vit dans le planificateur, pour que la prévisualisation et
l'application signalent les mêmes problèmes de la même façon, ligne par ligne.
Contraindre ici ferait répondre un 400 global à ce que la prévisualisation
présentait comme l'erreur d'une ligne précise.
"""

from __future__ import annotations

import uuid
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# Plafond de charge utile, distinct du plafond métier `referentiel_import_max_rows`
# (paramètres système) qu'applique le planificateur. Celui-ci n'existe que pour
# qu'un corps de requête absurde soit rejeté avant d'être désérialisé.
MAX_LIGNES_PAYLOAD = 20_000

ImportRowStatus = Literal["create", "update", "unchanged", "error"]


class ServiceImportRow(BaseModel):
    """Un service, tel que lu dans le classeur ou renvoyé par le dashboard."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ligne: int | None = Field(
        default=None, description="Ligne du classeur d'origine, pour situer une erreur."
    )
    code: str = Field(description="Identifiant court du service, unique dans l'organisation.")
    nom: str
    etage: str | None = None
    code_parent: str | None = Field(
        default=None,
        description=(
            "Code du service parent. Il peut désigner un service déjà en base **ou** "
            "un service créé par le même fichier."
        ),
    )


class AgentImportRow(BaseModel):
    """Une personne du référentiel, rattachée à un service par son code."""

    model_config = ConfigDict(str_strip_whitespace=True)

    ligne: int | None = None
    nom: str
    code_service: str = Field(
        description="Code du service de rattachement. Le service doit déjà exister."
    )
    fonction: str | None = Field(default=None, description="Alimente `role`.")
    bureau: str | None = Field(default=None, description="Alimente `office`.")


LigneImport = TypeVar("LigneImport", ServiceImportRow, AgentImportRow)


class ImportItem(BaseModel, Generic[LigneImport]):
    """Le sort d'une ligne : ce qu'elle deviendra, ou pourquoi elle bloque."""

    ligne: int | None = None
    statut: ImportRowStatus
    donnees: LigneImport
    entity_id: uuid.UUID | None = Field(
        default=None,
        description="Entrée existante reconnue, ou entrée créée après application.",
    )
    changements: dict[str, Any] | None = Field(
        default=None,
        description="Pour `update` : `{champ: {\"avant\": …, \"après\": …}}`.",
    )
    error_code: str | None = None
    message: str | None = None


class ImportReport(BaseModel, Generic[LigneImport]):
    """Plan avant application, ou compte rendu après — même forme dans les deux cas."""

    applique: bool = Field(
        description="`false` pour une prévisualisation : rien n'a été écrit."
    )
    organization_id: uuid.UUID
    organization_nom: str = Field(
        description="Pour que le dashboard nomme la structure visée avant de confirmer."
    )
    mettre_a_jour: bool
    total: int
    a_creer: int
    a_mettre_a_jour: int
    inchangees: int
    erreurs: int
    items: list[ImportItem[LigneImport]]


class ImportRequest(BaseModel, Generic[LigneImport]):
    """Corps de l'application : les lignes validées par l'administrateur."""

    organization_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Organisation qui reçoit l'import. Obligatoire pour l'ADMIN de "
            "plateforme — un service appartient toujours à une structure ; ignoré "
            "pour un administrateur d'organisation, borné à la sienne."
        ),
    )
    mettre_a_jour: bool = Field(
        default=False,
        description=(
            "Met à jour les entrées déjà présentes. À `false`, elles sont laissées "
            "intactes et comptées comme `unchanged` : les corrections faites au "
            "dashboard priment alors sur le fichier."
        ),
    )
    lignes: list[LigneImport] = Field(min_length=1, max_length=MAX_LIGNES_PAYLOAD)


class ServiceImportRequest(ImportRequest[ServiceImportRow]):
    pass


class AgentImportRequest(ImportRequest[AgentImportRow]):
    pass
