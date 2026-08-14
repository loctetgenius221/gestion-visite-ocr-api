from __future__ import annotations

from datetime import date

from sqlalchemy import Date, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DocumentType, Sexe


class Visitor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Visiteur externe identifié par sa pièce d'identité."""

    __tablename__ = "visitors"

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
