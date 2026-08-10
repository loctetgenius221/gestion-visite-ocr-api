"""Export du registre des visites (rôle ADMIN).

Le CSV est produit avec la bibliothèque standard : le registre est un tableau
plat, aucune dépendance ne se justifie. Le PDF, lui, en demanderait une — il est
donc annoncé comme indisponible plutôt que bâclé, la route et ses filtres étant
déjà en place pour l'accueillir.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Literal

from app.models.base import ensure_utc
from app.models.visit import Visit

ExportFormat = Literal["csv", "pdf"]

# En-têtes explicites plutôt que les noms de colonnes SQL : ce fichier est ouvert
# dans un tableur par des agents administratifs, pas par des développeurs.
_COLONNES: list[tuple[str, str]] = [
    ("id", "Identifiant visite"),
    ("statut", "Statut"),
    ("visiteur_nom", "Nom du visiteur"),
    ("visiteur_prenom", "Prénom du visiteur"),
    ("type_document", "Type de document"),
    ("numero_document", "N° de document"),
    ("nin", "NIN"),
    ("nationalite", "Nationalité"),
    ("service", "Service visité"),
    ("agent", "Personne rencontrée"),
    ("motif", "Motif"),
    ("badge", "N° de badge"),
    ("entree", "Entrée"),
    ("sortie", "Sortie"),
    ("duree_minutes", "Durée (minutes)"),
    ("enregistre_par", "Enregistré par"),
    ("cloture_par", "Clôturé par"),
    ("annulee_le", "Annulée le"),
    ("motif_annulation", "Motif d'annulation"),
]


def _horodatage(value: datetime | None) -> str:
    """ISO 8601, lisible par un humain comme par un tableur."""
    return value.isoformat() if value is not None else ""


def _ligne(visit: Visit) -> dict[str, str]:
    duree = ""
    if visit.checked_out_at is not None:
        # `ensure_utc` des deux côtés : selon le moteur, l'un peut être relu naïf
        # et l'autre conscient du fuseau, et la soustraction lèverait `TypeError`.
        ecart = ensure_utc(visit.checked_out_at) - ensure_utc(visit.checked_in_at)
        duree = str(round(ecart.total_seconds() / 60, 1))

    return {
        "id": str(visit.id),
        "statut": visit.statut.value,
        "visiteur_nom": visit.visitor.nom,
        "visiteur_prenom": visit.visitor.prenom,
        "type_document": visit.visitor.type_document.value,
        "numero_document": visit.visitor.numero_document,
        "nin": visit.visitor.nin or "",
        "nationalite": visit.visitor.nationalite or "",
        "service": visit.service.name,
        "agent": visit.agent.name,
        "motif": visit.purpose.libelle if visit.purpose else (visit.motif_libre or ""),
        "badge": visit.badge_number or "",
        "entree": _horodatage(visit.checked_in_at),
        "sortie": _horodatage(visit.checked_out_at),
        "duree_minutes": duree,
        "enregistre_par": visit.checked_in_user.identifiant,
        "cloture_par": visit.checked_out_user.identifiant if visit.checked_out_user else "",
        "annulee_le": _horodatage(visit.cancelled_at),
        "motif_annulation": visit.cancellation_reason or "",
    }


def visits_to_csv(visits: list[Visit]) -> bytes:
    """Sérialise les visites en CSV, encodé pour Excel.

    Deux précautions dictées par l'usage réel :

    - **BOM UTF-8** — sans lui, Excel sous Windows suppose l'encodage ANSI et
      abîme tous les accents des noms de visiteurs ;
    - **point-virgule** — séparateur attendu par Excel en configuration
      francophone, où la virgule est le séparateur décimal.
    """
    tampon = io.StringIO(newline="")
    writer = csv.DictWriter(
        tampon,
        fieldnames=[cle for cle, _ in _COLONNES],
        delimiter=";",
        quoting=csv.QUOTE_MINIMAL,
        extrasaction="ignore",
    )
    # Échappement explicite : un BOM littéral dans le source est invisible à la
    # relecture et se perd au premier outil qui normalise le fichier.
    tampon.write("\ufeff")
    writer.writerow({cle: libelle for cle, libelle in _COLONNES})
    for visit in visits:
        writer.writerow(_ligne(visit))
    return tampon.getvalue().encode("utf-8")


def export_filename(fmt: ExportFormat, now: datetime) -> str:
    return f"sigv-visites-{now:%Y%m%d-%H%M}.{fmt}"
