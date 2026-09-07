"""Ajout du rôle ORG_ADMIN et normalisation de l'administration globale."""

from collections.abc import Sequence

from alembic import op

revision: str = "e5b7c9d1a204"
down_revision: str | Sequence[str] | None = "d2f4a8b91c77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'ORG_ADMIN'")


def downgrade() -> None:
    # PostgreSQL ne permet pas de retirer une valeur d'ENUM sans reconstruire
    # le type. La migration est donc volontairement non destructive.
    pass
