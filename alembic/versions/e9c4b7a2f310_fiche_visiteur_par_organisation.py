"""Fiche visiteur par organisation (ADR-026, audit SEC-001)

La fiche `visitors` était commune à tout le parc : un enregistrement chez A
retrouvait la fiche saisie chez B par son numéro de pièce, la réécrivait, et
renvoyait à A le NIN, les coordonnées et les photos saisis par B.

1. l'unicité `(type_document, numero_document)` est levée ;
2. `visitors.organization_id` est ajouté ;
3. chaque fiche est **séparée** : elle revient à l'organisation de sa première
   visite, et chaque autre organisation où la personne est venue reçoit une
   copie, à laquelle ses visites et ses fiches de pièce oubliée sont
   rattachées. Une copie ne garde pas les photos rangées dans le dossier d'une
   autre organisation ;
4. l'unicité devient `(organization_id, type_document, numero_document)`.

Une fiche sans aucune visite (visite supprimée) reste sans organisation :
invisible de tous, elle sera purgée avec ses pièces (ADR-018).

Le downgrade refusionne les copies sur la plus ancienne fiche de chaque pièce.

Revision ID: e9c4b7a2f310
Revises: d8f1b3c5e7a9
Create Date: 2026-11-26
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "e9c4b7a2f310"
down_revision = "d8f1b3c5e7a9"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_ANCIENNE_UNICITE = "uq_visitors_type_document_numero_document"
_NOUVELLE_UNICITE = "uq_visitors_organization_id_type_document_numero_document"
_COLONNES_IMAGES = ("document_recto_url", "document_verso_url", "mrz_image_url")
# Fichier rangé par organisation : `<base>/org/<uuid>/...` (storage_urls.py).
_DOSSIER_ORGANISATION = re.compile(r"/org/([0-9a-fA-F-]{32,36})/")


def _uuid(valeur: Any) -> uuid.UUID:
    """UUID lu en base : objet natif sous PostgreSQL, hexadécimal sous SQLite."""
    return valeur if isinstance(valeur, uuid.UUID) else uuid.UUID(str(valeur))


def _nouvel_id(bind: sa.engine.Connection) -> Any:
    """Identifiant au format de stockage de `sa.Uuid` sur le moteur courant."""
    nouveau = uuid.uuid4()
    return nouveau if bind.dialect.name == "postgresql" else nouveau.hex


def _image_d_une_autre_organisation(url: str | None, organisation: Any) -> bool:
    if not url:
        return False
    trouve = _DOSSIER_ORGANISATION.search(url)
    return trouve is not None and _uuid(trouve.group(1)) != _uuid(organisation)


def _images_a_retirer(fiche: dict[str, Any], organisation: Any) -> dict[str, None]:
    return {
        colonne: None
        for colonne in _COLONNES_IMAGES
        if _image_d_une_autre_organisation(fiche.get(colonne), organisation)
    }


def _separer_les_fiches() -> None:
    bind = op.get_bind()
    passages = bind.execute(
        sa.text(
            "SELECT visitor_id, organization_id, MIN(checked_in_at) AS premiere "
            "FROM visits GROUP BY visitor_id, organization_id "
            "ORDER BY visitor_id, premiere"
        )
    ).all()
    organisations: dict[Any, list[Any]] = defaultdict(list)
    for visitor_id, organization_id, _ in passages:
        organisations[visitor_id].append(organization_id)

    copies = 0
    for visitor_id, orgs in organisations.items():
        fiche = dict(
            bind.execute(
                sa.text("SELECT * FROM visitors WHERE id = :id"), {"id": visitor_id}
            ).mappings().one()
        )
        premiere, *autres = orgs

        valeurs = {"organization_id": premiere, **_images_a_retirer(fiche, premiere)}
        affectations = ", ".join(f"{colonne} = :{colonne}" for colonne in valeurs)
        bind.execute(
            sa.text(f"UPDATE visitors SET {affectations} WHERE id = :id"),
            {**valeurs, "id": visitor_id},
        )

        for organisation in autres:
            copie = {
                **fiche,
                "id": _nouvel_id(bind),
                "organization_id": organisation,
                **_images_a_retirer(fiche, organisation),
            }
            colonnes = ", ".join(copie)
            parametres = ", ".join(f":{colonne}" for colonne in copie)
            bind.execute(
                sa.text(f"INSERT INTO visitors ({colonnes}) VALUES ({parametres})"), copie
            )
            for table in ("visits", "forgotten_document_cases"):
                bind.execute(
                    sa.text(
                        f"UPDATE {table} SET visitor_id = :copie "
                        "WHERE visitor_id = :origine AND organization_id = :organisation"
                    ),
                    {"copie": copie["id"], "origine": visitor_id, "organisation": organisation},
                )
            copies += 1

    if copies:
        logger.warning("Séparation des fiches visiteur : %s copie(s) créée(s)", copies)


def upgrade() -> None:
    with op.batch_alter_table("visitors") as batch:
        batch.drop_constraint(_ANCIENNE_UNICITE, type_="unique")
        batch.add_column(sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=True))
        batch.create_index(batch.f("ix_visitors_organization_id"), ["organization_id"])
        batch.create_foreign_key(
            batch.f("fk_visitors_organization_id_organizations"),
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    _separer_les_fiches()

    with op.batch_alter_table("visitors") as batch:
        batch.create_unique_constraint(
            _NOUVELLE_UNICITE, ["organization_id", "type_document", "numero_document"]
        )


# Fiche conservée pour une même pièce : la plus ancienne (c6e8a2d47f19).
_FICHE_CANONIQUE = (
    "SELECT k.id FROM visitors k "
    "WHERE k.type_document = {alias}.type_document "
    "AND k.numero_document = {alias}.numero_document "
    "ORDER BY k.created_at, k.id LIMIT 1"
)


def downgrade() -> None:
    with op.batch_alter_table("visitors") as batch:
        batch.drop_constraint(_NOUVELLE_UNICITE, type_="unique")

    for table in ("visits", "forgotten_document_cases"):
        op.execute(
            sa.text(
                f"UPDATE {table} SET visitor_id = ("
                "SELECT k.id FROM visitors k JOIN visitors d "
                "ON k.type_document = d.type_document "
                "AND k.numero_document = d.numero_document "
                f"WHERE d.id = {table}.visitor_id "
                "ORDER BY k.created_at, k.id LIMIT 1)"
            )
        )
    op.execute(
        sa.text(
            "DELETE FROM visitors WHERE id <> (" + _FICHE_CANONIQUE.format(alias="visitors") + ")"
        )
    )

    with op.batch_alter_table("visitors") as batch:
        batch.drop_constraint(
            batch.f("fk_visitors_organization_id_organizations"), type_="foreignkey"
        )
        batch.drop_index(batch.f("ix_visitors_organization_id"))
        batch.drop_column("organization_id")
        batch.create_unique_constraint(_ANCIENNE_UNICITE, ["type_document", "numero_document"])
