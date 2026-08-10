"""dashboard d'administration : rôles, sessions, audit, archivage, annulation

Extension du schéma pour le dashboard web :

- `users` — verrouillage après échecs de connexion et dernière connexion ;
- `user_sessions` — refresh tokens émis, pour lister et couper les sessions ;
- `audit_logs` — trace immuable des opérations sensibles ;
- `system_settings` — paramètres métier modifiables sans redéploiement ;
- `services` / `agents` / `purposes` — archivage logique, aucune suppression ;
- `visits` — annulation logique, et valeur `ANNULEE` ajoutée au type `visit_status`.

Toutes les colonnes ajoutées sont nullables ou dotées d'un `server_default` : la
migration s'applique sur une base en production sans interruption, et l'app mobile
existante n'en voit aucun effet.

Revision ID: a3d7e91c40b2
Revises: 5f1c5bd2957b
Create Date: 2026-08-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3d7e91c40b2'
down_revision: Union[str, Sequence[str], None] = '5f1c5bd2957b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type() -> sa.types.TypeEngine:
    """JSONB sur PostgreSQL, JSON ailleurs — SQLite ne connaît pas JSONB."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _est_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # --- Nouvelle valeur d'énumération ---------------------------------------
    if _est_postgres():
        # Reste **dans** la transaction d'Alembic : PostgreSQL 12+ accepte
        # `ADD VALUE` dans un bloc transactionnel, il interdit seulement d'utiliser
        # la nouvelle valeur avant le commit — ce que cette migration ne fait pas.
        # Un `COMMIT` préalable, nécessaire avant PostgreSQL 12, coûterait ici
        # l'atomicité : un échec plus bas laisserait le schéma à moitié migré.
        # `IF NOT EXISTS` rend l'opération rejouable.
        op.execute("ALTER TYPE visit_status ADD VALUE IF NOT EXISTS 'ANNULEE'")
    # Sur SQLite, l'énumération est un VARCHAR : rien à faire.

    # --- users : verrouillage ------------------------------------------------
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "failed_login_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    # --- Archivage des référentiels ------------------------------------------
    for table in ("services", "agents", "purposes"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "is_archived", sa.Boolean(), nullable=False, server_default=sa.false()
                )
            )
            batch_op.add_column(
                sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch_op.create_index(f"ix_{table}_is_archived", ["is_archived"], unique=False)

    # --- visits : annulation logique -----------------------------------------
    with op.batch_alter_table("visits") as batch_op:
        batch_op.add_column(sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("cancelled_by", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("cancellation_reason", sa.String(length=500), nullable=True))
        batch_op.create_foreign_key(
            "fk_visits_cancelled_by_users",
            "users",
            ["cancelled_by"],
            ["id"],
            ondelete="RESTRICT",
        )

    # --- user_sessions -------------------------------------------------------
    op.create_table(
        "user_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
    )
    op.create_index("ix_user_sessions_jti", "user_sessions", ["jti"], unique=True)
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False)

    # --- audit_logs ----------------------------------------------------------
    op.create_table(
        "audit_logs",
        # `SET NULL` : supprimer un compte ne doit pas effacer la trace de ce
        # qu'il a fait. Nul aussi pour les échecs de connexion, non authentifiés.
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_identifiant", sa.String(length=100), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("metadata", _json_type(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_audit_logs_actor_id_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"], unique=False)
    op.create_index(
        "ix_audit_logs_entity_entity_id", "audit_logs", ["entity", "entity_id"], unique=False
    )
    op.create_index(
        "ix_audit_logs_actor_id_created_at", "audit_logs", ["actor_id", "created_at"], unique=False
    )

    # --- system_settings -----------------------------------------------------
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", _json_type(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_system_settings_updated_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_system_settings"),
    )
    op.create_index("ix_system_settings_key", "system_settings", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_system_settings_key", table_name="system_settings")
    op.drop_table("system_settings")

    op.drop_index("ix_audit_logs_actor_id_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_entity_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_index("ix_user_sessions_jti", table_name="user_sessions")
    op.drop_table("user_sessions")

    with op.batch_alter_table("visits") as batch_op:
        batch_op.drop_constraint("fk_visits_cancelled_by_users", type_="foreignkey")
        batch_op.drop_column("cancellation_reason")
        batch_op.drop_column("cancelled_by")
        batch_op.drop_column("cancelled_at")

    for table in ("purposes", "agents", "services"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(f"ix_{table}_is_archived")
            batch_op.drop_column("archived_at")
            batch_op.drop_column("is_archived")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("last_login_at")
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_login_attempts")

    # La valeur 'ANNULEE' n'est pas retirée du type `visit_status` : PostgreSQL ne
    # sait pas supprimer une valeur d'énumération, il faudrait recréer le type et
    # réécrire la colonne. Une valeur inutilisée en trop est sans conséquence.
