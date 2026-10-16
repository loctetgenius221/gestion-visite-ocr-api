"""Durcissement pour le SaaS multi-organisations

1. `user_sessions.previous_jti` — le token remplacé lors d'un glissement est
   rattaché à sa session, pour être coupé avec elle (ADR-023) ;
2. `visits.client_reference` — unique par organisation, et non plus sur tout le
   parc : la clé d'idempotence d'un client ne bloque plus la synchro d'un autre ;
3. `visitors (type_document, numero_document)` — unicité posée en base (ADR-010),
   après fusion des doublons existants ;
4. PostgreSQL uniquement — index trigrammes (`pg_trgm`) pour les recherches
   `LIKE '%…%'` sur l'identité des visiteurs, qui parcouraient toute la table.

Revision ID: c6e8a2d47f19
Revises: a1c3e57d9b48
Create Date: 2026-10-16
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6e8a2d47f19"
down_revision: str | Sequence[str] | None = "a1c3e57d9b48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Fiche conservée pour une même pièce : la plus ancienne, qui porte l'historique.
_FICHE_CANONIQUE = (
    "SELECT k.id FROM visitors k "
    "WHERE k.type_document = {alias}.type_document "
    "AND k.numero_document = {alias}.numero_document "
    "ORDER BY k.created_at, k.id LIMIT 1"
)

_INDEX_TRIGRAMMES = {
    "ix_visitors_nom_trgm": "nom",
    "ix_visitors_prenom_trgm": "prenom",
    "ix_visitors_numero_document_trgm": "numero_document",
    "ix_visitors_nin_trgm": "nin",
}


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _fusionner_les_doublons_de_visiteurs() -> None:
    """Rattache visites et fiches de pièce oubliée à la fiche canonique, puis
    supprime les doublons.

    Les coordonnées propres à un doublon (téléphone, photos…) sont perdues : la
    fiche canonique garde les siennes. Le nombre de fiches fusionnées est
    journalisé pour être vérifié après la migration.
    """
    bind = op.get_bind()
    doublons = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM visitors v WHERE v.id <> ("
            + _FICHE_CANONIQUE.format(alias="v")
            + ")"
        )
    ).scalar_one()
    if not doublons:
        return

    logger.warning("Fusion de %s fiche(s) visiteur en double", doublons)
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
            "DELETE FROM visitors WHERE id <> ("
            + _FICHE_CANONIQUE.format(alias="visitors")
            + ")"
        )
    )


def upgrade() -> None:
    # --- 1. Token remplacé rattaché à sa session -----------------------------
    op.add_column(
        "user_sessions", sa.Column("previous_jti", sa.String(length=64), nullable=True)
    )
    op.create_index(
        op.f("ix_user_sessions_previous_jti"), "user_sessions", ["previous_jti"], unique=False
    )

    # --- 2. Idempotence de la synchro, par organisation ----------------------
    with op.batch_alter_table("visits") as batch:
        batch.drop_index("ix_visits_client_reference")
        batch.create_index("ix_visits_client_reference", ["client_reference"], unique=False)
        batch.create_unique_constraint(
            "uq_visits_organization_id_client_reference",
            ["organization_id", "client_reference"],
        )

    # --- 3. Unicité de la pièce d'identité -----------------------------------
    _fusionner_les_doublons_de_visiteurs()
    with op.batch_alter_table("visitors") as batch:
        batch.create_unique_constraint(
            "uq_visitors_type_document_numero_document",
            ["type_document", "numero_document"],
        )

    # --- 4. Recherche plein texte sur l'identité (PostgreSQL) ----------------
    # Index d'expression sur `lower(colonne)` : c'est la forme exacte des
    # requêtes du dépôt. Absents des modèles — Alembic ignore les index
    # d'expression à l'autogénération — et donc créés ici seulement.
    if _is_postgres():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for nom, colonne in _INDEX_TRIGRAMMES.items():
            op.execute(
                f"CREATE INDEX IF NOT EXISTS {nom} ON visitors "
                f"USING gin (lower({colonne}) gin_trgm_ops)"
            )


def downgrade() -> None:
    if _is_postgres():
        for nom in _INDEX_TRIGRAMMES:
            op.execute(f"DROP INDEX IF EXISTS {nom}")

    # Les doublons fusionnés ne sont pas recréés : l'information est perdue.
    with op.batch_alter_table("visitors") as batch:
        batch.drop_constraint("uq_visitors_type_document_numero_document", type_="unique")

    # Échoue si deux organisations ont depuis utilisé la même référence : il
    # faudrait alors arbitrer à la main, ce qu'aucun downgrade ne peut décider.
    with op.batch_alter_table("visits") as batch:
        batch.drop_constraint("uq_visits_organization_id_client_reference", type_="unique")
        batch.drop_index("ix_visits_client_reference")
        batch.create_index("ix_visits_client_reference", ["client_reference"], unique=True)

    op.drop_index(op.f("ix_user_sessions_previous_jti"), table_name="user_sessions")
    op.drop_column("user_sessions", "previous_jti")
