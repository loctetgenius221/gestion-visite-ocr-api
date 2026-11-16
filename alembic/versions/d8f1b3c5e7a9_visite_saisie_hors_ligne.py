"""Visite saisie hors ligne

Marque les entrées transmises par `POST /visits/sync` (ADR-025), pour que le
poste de contrôle les reconnaisse d'un coup d'œil. Ajout seul, avec une valeur
par défaut : la version précédente du code continue de fonctionner pendant le
déploiement.

Revision ID: d8f1b3c5e7a9
Revises: c6e8a2d47f19
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d8f1b3c5e7a9"
down_revision = "c6e8a2d47f19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("visits") as batch:
        batch.add_column(
            sa.Column(
                "recorded_offline",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("visits") as batch:
        batch.drop_column("recorded_offline")
