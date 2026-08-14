from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.enums import DocumentType, Sexe, VisitStatus
from app.schemas.auth import UserRead
from app.schemas.common import ORMModel
from app.schemas.referentiel import AgentRead, PurposeRead, ServiceRead


class VisitorInput(BaseModel):
    """Identité du visiteur, pré-remplie par l'OCR MRZ puis corrigeable par l'agent."""

    prenom: str = Field(min_length=1, max_length=150)
    nom: str = Field(min_length=1, max_length=150)
    type_document: DocumentType
    numero_document: str = Field(min_length=1, max_length=60)
    nin: str | None = Field(
        default=None,
        max_length=30,
        description=(
            "Numéro d'Identification National. Absent du MRZ des CNI sénégalaises "
            "(ADR-005), il est lu par OCR sur la zone imprimée (ADR-014). "
            "Alphanumérique : son code d'état civil peut porter une lettre, "
            "« 2 K05 2012 00108 » (ADR-016). Séparateurs et espaces sont retirés."
        ),
    )
    nationalite: str | None = Field(default=None, max_length=60)
    date_naissance: date | None = None
    sexe: Sexe | None = None
    date_expiration_document: date | None = None
    telephone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    provenance: str | None = Field(default=None, max_length=200)
    immatriculation_vehicule: str | None = Field(default=None, max_length=40)
    document_recto_url: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Photo du recto, déposée via `POST /uploads/document?face=recto`. "
            "Utile en saisie manuelle : le scan MRZ ne capture que le verso."
        ),
    )
    document_verso_url: str | None = Field(
        default=None,
        max_length=500,
        description="Photo du verso — la face portant le MRZ sur une CNI.",
    )
    mrz_image_url: str | None = Field(
        default=None,
        max_length=500,
        deprecated="Utilisez `document_verso_url`, qui désigne la même face.",
        description=(
            "Déprécié. Conservé le temps que l'app mobile bascule : à défaut de "
            "`document_verso_url`, cette valeur y est reportée."
        ),
    )

    @model_validator(mode="after")
    def _reporter_image_mrz(self) -> VisitorInput:
        """`mrz_image_url` alimente `document_verso_url` : c'est la même face.

        Sans ce report, un client resté sur l'ancien champ verrait le verso
        disparaître de la nouvelle colonne — et la purge des images (ADR-018)
        raisonnerait sur une donnée incomplète.
        """
        if self.document_verso_url is None and self.mrz_image_url is not None:
            self.document_verso_url = self.mrz_image_url
        return self

    @field_validator("nin", mode="after")
    @classmethod
    def _normaliser_nin(cls, valeur: str | None) -> str | None:
        """Aligne la saisie manuelle sur la sortie OCR : majuscules, sans séparateurs.

        Le NIN est imprimé par blocs (« 2 K05 2012 00108 ») et l'agent le recopie
        souvent tel quel, là où l'OCR renvoie une suite compacte. Sans cette
        normalisation, la même personne porterait deux valeurs différentes dans une
        colonne indexée et destinée à la recherche.
        """
        if valeur is None:
            return None
        return re.sub(r"[^A-Z0-9]", "", valeur.upper()) or None


class VisitorPassageInput(BaseModel):
    """Ce qui change d'un passage à l'autre, pour un visiteur déjà connu.

    Ces trois informations vivent sur la fiche visiteur (voir ADR-010) alors
    qu'elles appartiennent au passage : la plaque du véhicule d'il y a trois mois
    n'a rien à voir avec celle du jour. Les recopier telles quelles en reprenant une
    fiche connue produirait une donnée fausse — d'où ce bloc de mise à jour, qui
    rafraîchit la fiche au moment de l'enregistrement.

    Limite assumée : la fiche ne garde que la dernière valeur, l'historique par
    visite n'est pas reconstituable (ADR-017).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    telephone: str | None = Field(default=None, max_length=40)
    provenance: str | None = Field(default=None, max_length=200)
    immatriculation_vehicule: str | None = Field(default=None, max_length=40)


class VisitorRead(ORMModel):
    id: uuid.UUID
    prenom: str
    nom: str
    type_document: DocumentType
    numero_document: str
    nin: str | None = None
    nationalite: str | None = None
    date_naissance: date | None = None
    sexe: Sexe | None = None
    date_expiration_document: date | None = None
    telephone: str | None = None
    email: str | None = None
    provenance: str | None = None
    immatriculation_vehicule: str | None = None
    document_recto_url: str | None = None
    document_verso_url: str | None = None
    mrz_image_url: str | None = None


class VisitorSearchResult(VisitorRead):
    """Fiche connue, renvoyée par la recherche de visiteurs.

    Les deux champs ajoutés évitent un aller-retour : le client affiche la dernière
    venue, et propose la clôture au lieu de se heurter à un `VISITOR_ALREADY_PRESENT`
    en enregistrant une personne déjà présente.
    """

    derniere_visite_at: datetime | None = Field(
        default=None, description="Entrée de la dernière visite non annulée."
    )
    visite_ouverte_id: uuid.UUID | None = Field(
        default=None, description="Visite encore `PRESENT`, s'il y en a une."
    )


class VisitCreate(BaseModel):
    """Payload de création d'une visite complète (visiteur + contexte).

    L'identité arrive de deux façons, exclusives l'une de l'autre :

    - `visitor` : identité complète, issue du scan MRZ ou saisie par l'agent ;
    - `visitor_id` : fiche déjà connue, pour une personne qui revient — sans
      rescanner sa pièce. `visitor_passage` rafraîchit alors ce qui a changé.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    visitor: VisitorInput | None = None
    visitor_id: uuid.UUID | None = None
    visitor_passage: VisitorPassageInput | None = None
    service_id: uuid.UUID
    agent_id: uuid.UUID
    purpose_id: uuid.UUID | None = None
    motif_libre: str | None = Field(default=None, max_length=500)
    badge_number: str | None = Field(default=None, max_length=50)
    signature_url: str | None = Field(default=None, max_length=500)
    # Horodatage d'entrée : fourni par le client en mode hors-ligne, sinon `now()` serveur.
    checked_in_at: datetime | None = None
    # Clé d'idempotence pour la synchronisation batch (voir ADR-004).
    client_reference: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _identite_requise(self) -> VisitCreate:
        """Exactement une source d'identité, jamais deux.

        Accepter les deux obligerait à trancher un désaccord entre la fiche
        référencée et l'identité fournie — un arbitrage qu'aucune règle ne rend
        évident, et qui se solderait par une identité erronée au registre.
        """
        if (self.visitor is None) == (self.visitor_id is None):
            raise ValueError(
                "Renseignez `visitor` (identité scannée ou saisie) ou `visitor_id` "
                "(visiteur déjà connu), mais pas les deux."
            )
        if self.visitor_passage is not None and self.visitor is not None:
            raise ValueError(
                "`visitor_passage` accompagne `visitor_id`. Avec `visitor`, ces "
                "champs se renseignent directement dans le bloc `visitor`."
            )
        return self

    @model_validator(mode="after")
    def _motif_requis(self) -> VisitCreate:
        """Un motif est obligatoire : soit une entrée du référentiel, soit du texte libre."""
        if self.purpose_id is None and not (self.motif_libre and self.motif_libre.strip()):
            raise ValueError("Un motif est requis : renseignez `purpose_id` ou `motif_libre`.")
        return self


class VisitRead(ORMModel):
    id: uuid.UUID
    statut: VisitStatus
    motif_libre: str | None = None
    badge_number: str | None = None
    signature_url: str | None = None
    checked_in_at: datetime
    checked_out_at: datetime | None = None
    client_reference: str | None = None
    created_at: datetime
    updated_at: datetime

    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None

    visitor: VisitorRead
    service: ServiceRead
    agent: AgentRead
    purpose: PurposeRead | None = None
    checked_in_user: UserRead
    checked_out_user: UserRead | None = None
    cancelled_user: UserRead | None = None


class VisitUpdate(BaseModel):
    """Correction d'une visite par un administrateur (erreur de saisie).

    Le `reason` est obligatoire : c'est lui qui rend la modification défendable
    lors d'un audit. Il n'est pas stocké sur la visite mais dans le journal, avec
    le diff avant/après.

    L'identité du visiteur n'est pas modifiable ici : elle provient du scan MRZ et
    la corriger relèverait d'un autre geste métier, avec sa propre traçabilité.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=500)

    service_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    purpose_id: uuid.UUID | None = None
    motif_libre: str | None = Field(default=None, max_length=500)
    badge_number: str | None = Field(default=None, max_length=50)
    checked_in_at: datetime | None = None
    checked_out_at: datetime | None = None

    @model_validator(mode="after")
    def _au_moins_un_champ(self) -> VisitUpdate:
        modifiables = self.model_dump(exclude={"reason"}, exclude_unset=True)
        if not modifiables:
            raise ValueError("Aucun champ à modifier n'a été fourni.")
        return self


class VisitCancelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=500)


class VisitFilters(BaseModel):
    statut: VisitStatus | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    search: str | None = Field(default=None, max_length=100)
    sort: Literal["asc", "desc"] = "desc"

    # Filtres du dashboard web. Sans effet sur l'app mobile, qui ne les envoie pas.
    service_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    purpose_id: uuid.UUID | None = None
    # Auteur de l'enregistrement : correspond à `visits.checked_in_by`.
    created_by: uuid.UUID | None = None


class VisitSyncRequest(BaseModel):
    visits: list[VisitCreate] = Field(min_length=1, max_length=200)


class VisitSyncItemResult(BaseModel):
    index: int = Field(description="Position de la visite dans le batch envoyé.")
    client_reference: str | None = None
    status: Literal["created", "conflict", "error"]
    visit_id: uuid.UUID | None = None
    error_code: str | None = None
    message: str | None = None


class VisitSyncResponse(BaseModel):
    total: int
    created: int
    conflicts: int
    errors: int
    results: list[VisitSyncItemResult]
