"""Schémas des organisations et structures.

`logo_url` est en **lecture seule** partout : il ne s'écrit que par
`POST /organizations/{id}/logo`, qui valide le contenu du fichier et le range
sous notre propre domaine. Exposé en écriture, le champ laisserait un
administrateur d'organisation placer une ressource tierce — donc un traceur —
sur chaque écran du dashboard, ou une `javascript:` URL dans un `<img src>`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.schemas.common import ORMModel
from app.schemas.user import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, UserAdminRead

# Couleur hexadécimale, seule forme que le dashboard sait rendre. Le motif est
# partagé plutôt que recopié sur chacun des trois champs.
CouleurHex = Annotated[
    str,
    StringConstraints(pattern=r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", max_length=20),
]

# Le code sert d'identifiant lisible (URL, exports, rapprochement d'annuaires) :
# on le restreint à ce qui reste stable une fois recopié à la main.
CodeOrganisation = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9\-_]{1,49}$", strip_whitespace=True),
]


class OrganizationThemeRead(ORMModel):
    """Charte graphique d'une organisation.

    Servie **avant authentification** par `/organizations/theme` : la page de
    connexion doit porter les couleurs et le logo de la structure, et elle
    s'affiche par définition sans token. Ne contient donc que ce qui est déjà
    visible sur une devanture — jamais de contact ni de statistique.
    """

    id: uuid.UUID
    nom: str
    code: str
    logo_url: str | None = None
    primary_color: str | None = Field(default=None, description="Couleur principale (ex: #1E3A8A)")
    secondary_color: str | None = Field(default=None, description="Couleur secondaire")
    accent_color: str | None = Field(default=None, description="Couleur d'accent")
    theme_config: dict[str, Any] | None = Field(
        default=None,
        description="Réglages graphiques complémentaires (message d'accueil, options de thème…).",
    )


class OrganizationRead(ORMModel):
    """Vue détaillée d'une organisation ou structure."""

    id: uuid.UUID
    nom: str
    code: str
    localisation: str | None = None
    description: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    is_active: bool
    logo_url: str | None = Field(
        default=None, description="Écrit uniquement par `POST /organizations/{id}/logo`."
    )
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    theme_config: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class OrganizationSummary(ORMModel):
    """Vue allégée pour les sélecteurs et les listes."""

    id: uuid.UUID
    nom: str
    code: str
    localisation: str | None = None
    logo_url: str | None = None
    is_active: bool


class OrganizationCreate(BaseModel):
    """Création d'une structure, avec option de créer son premier administrateur."""

    model_config = ConfigDict(str_strip_whitespace=True)

    nom: str = Field(min_length=2, max_length=200, description="Nom officiel de la structure")
    code: CodeOrganisation = Field(
        description=(
            "Code unique, lettres/chiffres/tiret (ex: MIN-INTERIEUR). "
            "Normalisé en majuscules."
        )
    )
    localisation: str | None = Field(default=None, max_length=300)
    description: str | None = None
    contact_email: EmailStr | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)

    primary_color: CouleurHex | None = None
    secondary_color: CouleurHex | None = None
    accent_color: CouleurHex | None = None
    theme_config: dict[str, Any] | None = None

    # Premier administrateur de la structure. Facultatif, mais fortement conseillé :
    # sans lui, seul l'administrateur de plateforme peut agir sur l'organisation.
    admin_nom: str | None = Field(default=None, max_length=150)
    admin_identifiant: str | None = Field(default=None, min_length=3, max_length=100)
    admin_mot_de_passe: str | None = Field(
        default=None,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        description="Omis, le serveur en génère un et le renvoie une seule fois.",
    )


class OrganizationUpdate(BaseModel):
    """Mise à jour partielle : seuls les champs **présents** dans la requête bougent.

    Un `null` explicite efface le champ ; un champ absent est laissé tel quel.
    `is_active` n'est pas ici : l'activation passe par
    `PATCH /organizations/{id}/status`, qui vérifie les visites en cours.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    nom: str | None = Field(default=None, min_length=2, max_length=200)
    code: CodeOrganisation | None = None
    localisation: str | None = Field(default=None, max_length=300)
    description: str | None = None
    contact_email: EmailStr | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)

    primary_color: CouleurHex | None = None
    secondary_color: CouleurHex | None = None
    accent_color: CouleurHex | None = None
    theme_config: dict[str, Any] | None = None


class OrganizationStatusUpdate(BaseModel):
    """Activation ou désactivation d'une organisation."""

    is_active: bool = Field(description="`false` coupe l'accès de tous les comptes rattachés.")
    motif: str | None = Field(
        default=None,
        max_length=300,
        description="Raison consignée au journal d'audit (fin de convention, suspension…).",
    )


class OrganizationAdminCreate(BaseModel):
    """Nomination d'un administrateur sur une organisation existante."""

    model_config = ConfigDict(str_strip_whitespace=True)

    nom: str = Field(min_length=1, max_length=150)
    identifiant: str = Field(min_length=3, max_length=100)
    poste: str | None = Field(default=None, max_length=150)
    mot_de_passe: str | None = Field(
        default=None,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        description="Omis, le serveur en génère un et le renvoie une seule fois.",
    )


class OrganizationFilters(BaseModel):
    """Filtres de la liste des organisations."""

    search: str | None = Field(
        default=None, max_length=100, description="Nom, code ou localisation."
    )
    is_active: bool | None = None
    sort: Literal["asc", "desc"] = "asc"
    sort_by: Literal["nom", "created_at"] = "nom"
    # Renseigné par le service pour un appelant non global : il ne voit que la sienne.
    organization_id: uuid.UUID | None = None


class OrganizationStats(BaseModel):
    """Volumétrie d'une organisation."""

    organization_id: uuid.UUID
    total_services: int
    total_agents: int
    total_users: int
    total_visits: int
    active_visits_count: int


class OrganizationWithAdminResponse(BaseModel):
    """Réponse de création d'une organisation, avec son administrateur éventuel."""

    organization: OrganizationRead
    admin_user: UserAdminRead | None = None
    admin_temporary_password: str | None = Field(
        default=None,
        description=(
            "Mot de passe généré par le serveur, renvoyé **une seule fois**. "
            "Il n'est stocké nulle part en clair et sera irrécupérable ensuite."
        ),
    )


class OrganizationLogoResponse(BaseModel):
    """URL du logo après dépôt."""

    logo_url: str = Field(description="À utiliser telle quelle dans un `<img src>`.")
