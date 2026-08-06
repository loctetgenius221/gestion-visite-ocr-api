from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.models.enums import DocumentType, MrzFormat, Sexe


class MrzFields(BaseModel):
    """Champs d'identité extraits du MRZ (spec §5.5)."""

    nom: str
    prenom: str
    numero_document: str
    # Pour une CNI sénégalaise le NIN est encodé dans la zone `optional_data` du TD1 ;
    # à défaut on retombe sur `numero_document`. Voir ADR-005.
    nin: str | None = None
    nationalite: str | None = None
    date_naissance: date | None = None
    sexe: Sexe | None = None
    date_expiration: date | None = None
    pays_emetteur: str | None = None


class ChecksumDetails(BaseModel):
    """Résultat des vérifications de checksums ICAO 9303."""

    document_number: bool
    date_of_birth: bool
    expiration_date: bool
    composite: bool


class MrzScanResponse(BaseModel):
    document_type: DocumentType
    mrz_format: MrzFormat
    mrz_valid: bool
    fields: MrzFields
    checksum_details: ChecksumDetails
    raw_mrz_lines: list[str]
    # Chemin de l'image conservée pour audit (non renvoyé si le stockage est désactivé).
    mrz_image_url: str | None = None
    processing_time_ms: int | None = Field(
        default=None, description="Durée du pipeline OCR côté serveur, en millisecondes."
    )
