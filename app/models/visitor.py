from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DocumentType, Sexe


class Visitor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Visiteur externe identifié par sa pièce d'identité."""

    __tablename__ = "visitors"
    __table_args__ = (
        # ADR-010 : une personne = un couple (type, numéro de pièce) **dans une
        # organisation** (ADR-026). Sans la contrainte, deux synchronisations
        # simultanées créaient deux fiches.
        UniqueConstraint(
            "organization_id",
            "type_document",
            "numero_document",
            name="uq_visitors_organization_id_type_document_numero_document",
        ),
    )

    # Chaque organisation tient ses propres fiches (ADR-026) : une saisie chez A
    # ne lit ni ne réécrit jamais la fiche de la même personne chez B. Nullable
    # pour les seules fiches orphelines d'avant la séparation (aucune visite à
    # rattacher) : invisibles de toute organisation, elles disparaissent avec la
    # purge des pièces.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    prenom: Mapped[str] = mapped_column(String(150), nullable=False)
    nom: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    type_document: Mapped[DocumentType] = mapped_column(
        SAEnum(DocumentType, name="document_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # Le couple (type_document, numero_document) identifie un visiteur : on indexe
    # le numéro pour permettre la recherche et la déduplication lors des synchros offline.
    numero_document: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    # Numéro d'Identification National : imprimé au verso de la CNI sénégalaise mais
    # **absent du MRZ** (vérifié sur une carte réelle, voir ADR-005). Il est extrait
    # de la ligne imprimée juste au-dessus de la bande MRZ, dans la même passe OCR
    # (ADR-014) : aucune saisie manuelle n'est requise.
    nin: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    nationalite: Mapped[str | None] = mapped_column(String(60), nullable=True)
    date_naissance: Mapped[date | None] = mapped_column(Date, nullable=True)
    sexe: Mapped[Sexe | None] = mapped_column(
        SAEnum(Sexe, name="sexe", values_callable=lambda e: [m.value for m in e]), nullable=True
    )
    date_expiration_document: Mapped[date | None] = mapped_column(Date, nullable=True)
    telephone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provenance: Mapped[str | None] = mapped_column(String(200), nullable=True)
    immatriculation_vehicule: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Images de la pièce, conservées pour audit (ADR-018).
    #
    # Le scan MRZ ne capture que la face portant le MRZ — le verso d'une CNI. En
    # saisie manuelle, quand l'OCR échoue, l'agent photographie les deux faces :
    # le recto porte la photo et les mentions que le verso n'a pas.
    document_recto_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    document_verso_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Déprécié au profit de `document_verso_url`, qui désigne la même face. Conservé
    # en écriture le temps que l'app mobile bascule : le retirer maintenant casserait
    # toutes les tablettes déjà déployées.
    mrz_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - aide au debug uniquement
        return f"<Visitor {self.nom} {self.prenom}>"
