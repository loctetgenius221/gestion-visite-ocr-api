"""Cloisonnement multi-organisations : audit, réglages, motifs et contraintes

Consolide le support multi-organisations posé par `f8a1c9e30b12` :

1. `audit_logs.organization_id` — le journal était global, un administrateur
   d'organisation y lisait l'activité de toutes les structures ;
2. `system_settings.organization_id` — la table clé/valeur était unique pour la
   plateforme, chaque structure règle désormais son exploitation ;
3. `purposes.organization_id` — le libellé était unique globalement : la seconde
   structure à vouloir un motif « Réunion » ne pouvait pas le créer ;
4. `uq_services_organization_id_code` — la migration multi-organisations avait
   retiré l'index unique sur `services.code` sans le remplacer, autorisant deux
   services de même code dans une même organisation ;
5. `services.organization_id` et `visits.organization_id` passent `NOT NULL` :
   le backfill est acquis, et une ligne sans organisation serait invisible de
   tous les postes de contrôle.

Revision ID: a1c3e57d9b48
Revises: e5b7c9d1a204
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c3e57d9b48"
down_revision: str | Sequence[str] | None = "e5b7c9d1a204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. Journal d'audit rattaché à une organisation ----------------------
    op.add_column("audit_logs", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_audit_logs_organization_id"), "audit_logs", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_audit_logs_organization_id_created_at",
        "audit_logs",
        ["organization_id", "created_at"],
        unique=False,
    )
    with op.batch_alter_table("audit_logs") as batch:
        batch.create_foreign_key(
            batch.f("fk_audit_logs_organization_id_organizations"),
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Rattache l'historique à l'organisation de son auteur. Les traces sans acteur
    # — échecs de connexion, tâches planifiées — restent globales : leur périmètre
    # est inconnu, et l'inventer ferait apparaître dans le journal d'une structure
    # des événements qui ne la concernent pas.
    op.execute(
        sa.text(
            "UPDATE audit_logs SET organization_id = ("
            "  SELECT users.organization_id FROM users WHERE users.id = audit_logs.actor_id"
            ") WHERE actor_id IS NOT NULL AND organization_id IS NULL"
        )
    )

    # --- 2. Réglages par organisation ---------------------------------------
    op.add_column("system_settings", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_system_settings_organization_id"),
        "system_settings",
        ["organization_id"],
        unique=False,
    )
    with op.batch_alter_table("system_settings") as batch:
        # L'unicité porte désormais sur le couple : une même clé existe une fois
        # pour la plateforme, et une fois par organisation qui la surcharge.
        batch.drop_index("ix_system_settings_key")
        batch.create_index(batch.f("ix_system_settings_key"), ["key"], unique=False)
        batch.create_unique_constraint(
            "uq_system_settings_organization_id_key", ["organization_id", "key"]
        )
        batch.create_foreign_key(
            batch.f("fk_system_settings_organization_id_organizations"),
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # --- 3. Motifs de visite par organisation --------------------------------
    op.add_column("purposes", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_purposes_organization_id"), "purposes", ["organization_id"], unique=False
    )
    with op.batch_alter_table("purposes") as batch:
        # Les motifs existants deviennent **communs** (`organization_id IS NULL`) :
        # ils étaient partagés par tout le parc, les rattacher d'office à la
        # première organisation les retirerait à toutes les autres.
        batch.drop_constraint("uq_purposes_libelle", type_="unique")
        batch.create_unique_constraint(
            "uq_purposes_organization_id_libelle", ["organization_id", "libelle"]
        )
        batch.create_foreign_key(
            batch.f("fk_purposes_organization_id_organizations"),
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    # --- 4. Unicité du code de service dans son organisation -----------------
    with op.batch_alter_table("services") as batch:
        batch.create_unique_constraint(
            "uq_services_organization_id_code", ["organization_id", "code"]
        )

    # --- 5. Rattachement obligatoire des services et des visites -------------
    # Filet de sécurité : `f8a1c9e30b12` a backfillé, mais une ligne a pu être
    # insérée sans organisation entre les deux migrations.
    organisation_par_defaut = "(SELECT id FROM organizations ORDER BY created_at LIMIT 1)"
    for table in ("services", "visits"):
        op.execute(
            sa.text(
                f"UPDATE {table} SET organization_id = {organisation_par_defaut} "
                "WHERE organization_id IS NULL"
            )
        )

    with op.batch_alter_table("services") as batch:
        batch.alter_column("organization_id", existing_type=sa.Uuid(), nullable=False)
    with op.batch_alter_table("visits") as batch:
        batch.alter_column("organization_id", existing_type=sa.Uuid(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("visits") as batch:
        batch.alter_column("organization_id", existing_type=sa.Uuid(), nullable=True)
    with op.batch_alter_table("services") as batch:
        batch.alter_column("organization_id", existing_type=sa.Uuid(), nullable=True)
        batch.drop_constraint("uq_services_organization_id_code", type_="unique")

    with op.batch_alter_table("purposes") as batch:
        batch.drop_constraint(
            batch.f("fk_purposes_organization_id_organizations"), type_="foreignkey"
        )
        batch.drop_constraint("uq_purposes_organization_id_libelle", type_="unique")
        batch.create_unique_constraint("uq_purposes_libelle", ["libelle"])
    op.drop_index(op.f("ix_purposes_organization_id"), table_name="purposes")
    op.drop_column("purposes", "organization_id")

    with op.batch_alter_table("system_settings") as batch:
        batch.drop_constraint(
            batch.f("fk_system_settings_organization_id_organizations"), type_="foreignkey"
        )
        batch.drop_constraint("uq_system_settings_organization_id_key", type_="unique")
        batch.drop_index(batch.f("ix_system_settings_key"))
        batch.create_index("ix_system_settings_key", ["key"], unique=True)
    op.drop_index(op.f("ix_system_settings_organization_id"), table_name="system_settings")
    op.drop_column("system_settings", "organization_id")

    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_constraint(
            batch.f("fk_audit_logs_organization_id_organizations"), type_="foreignkey"
        )
    op.drop_index("ix_audit_logs_organization_id_created_at", table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_organization_id"), table_name="audit_logs")
    op.drop_column("audit_logs", "organization_id")
