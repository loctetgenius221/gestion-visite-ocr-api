"""support multi-organisations et structures

Création de la table organizations et rattachement des utilisateurs,
services et visites a une organisation specifique.
Rattachement automatique des donnees existantes a une organisation par defaut.

Revision ID: f8a1c9e30b12
Revises: c9a4d13f6e27
Create Date: 2026-09-01
"""
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f8a1c9e30b12'
down_revision: str | Sequence[str] | None = 'c9a4d13f6e27'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    # 1. Création de la table organizations
    op.create_table(
        'organizations',
        sa.Column('id', sa.Uuid(as_uuid=True), nullable=False),
        sa.Column('nom', sa.String(length=200), nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('localisation', sa.String(length=300), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('contact_email', sa.String(length=255), nullable=True),
        sa.Column('contact_phone', sa.String(length=40), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    )
    op.create_index(op.f('ix_organizations_code'), 'organizations', ['code'], unique=True)
    op.create_index(op.f('ix_organizations_nom'), 'organizations', ['nom'], unique=False)

    # 2. Insertion de l'organisation par défaut pour garantir la rétrocompatibilité
    bind = op.get_bind()
    now = datetime.now(UTC)
    org_table = sa.table(
        'organizations',
        sa.column('id', sa.Uuid),
        sa.column('nom', sa.String),
        sa.column('code', sa.String),
        sa.column('localisation', sa.String),
        sa.column('description', sa.Text),
        sa.column('is_active', sa.Boolean),
        sa.column('created_at', sa.DateTime(timezone=True)),
        sa.column('updated_at', sa.DateTime(timezone=True)),
    )
    bind.execute(
        org_table.insert().values(
            id=DEFAULT_ORG_ID,
            nom="Organisation Principale",
            code="DEFAULT",
            localisation="Siège",
            description="Structure par défaut créée lors de la migration multi-organisations",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )

    # 3. Ajout de organization_id sur users
    op.add_column('users', sa.Column('organization_id', sa.Uuid(as_uuid=True), nullable=True))
    op.create_index(op.f('ix_users_organization_id'), 'users', ['organization_id'], unique=False)
    with op.batch_alter_table('users') as batch_op:
        batch_op.create_foreign_key(
            batch_op.f('fk_users_organization_id_organizations'),
            'organizations',
            ['organization_id'],
            ['id'],
            ondelete='RESTRICT',
        )

    # 4. Ajout de organization_id sur services et assouplissement de l'index code
    op.add_column('services', sa.Column('organization_id', sa.Uuid(as_uuid=True), nullable=True))
    op.create_index(op.f('ix_services_organization_id'), 'services', ['organization_id'], unique=False)
    with op.batch_alter_table('services') as batch_op:
        batch_op.drop_index('ix_services_code')
        batch_op.create_index(batch_op.f('ix_services_code'), ['code'], unique=False)
        batch_op.create_foreign_key(
            batch_op.f('fk_services_organization_id_organizations'),
            'organizations',
            ['organization_id'],
            ['id'],
            ondelete='RESTRICT',
        )

    # 5. Ajout de organization_id sur visits
    op.add_column('visits', sa.Column('organization_id', sa.Uuid(as_uuid=True), nullable=True))
    op.create_index(op.f('ix_visits_organization_id'), 'visits', ['organization_id'], unique=False)
    op.create_index('ix_visits_org_statut_checked_in', 'visits', ['organization_id', 'statut', 'checked_in_at'], unique=False)
    with op.batch_alter_table('visits') as batch_op:
        batch_op.create_foreign_key(
            batch_op.f('fk_visits_organization_id_organizations'),
            'organizations',
            ['organization_id'],
            ['id'],
            ondelete='RESTRICT',
        )

    # 6. Backfill des données existantes vers l'organisation par défaut
    bind.execute(
        sa.text("UPDATE users SET organization_id = :org_id WHERE organization_id IS NULL").bindparams(org_id=DEFAULT_ORG_ID)
    )
    bind.execute(
        sa.text("UPDATE services SET organization_id = :org_id WHERE organization_id IS NULL").bindparams(org_id=DEFAULT_ORG_ID)
    )
    bind.execute(
        sa.text("UPDATE visits SET organization_id = :org_id WHERE organization_id IS NULL").bindparams(org_id=DEFAULT_ORG_ID)
    )


def downgrade() -> None:
    with op.batch_alter_table('visits') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_visits_organization_id_organizations'), type_='foreignkey')
        batch_op.drop_index('ix_visits_org_statut_checked_in')
        batch_op.drop_index(batch_op.f('ix_visits_organization_id'))
        batch_op.drop_column('organization_id')

    with op.batch_alter_table('services') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_services_organization_id_organizations'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_services_organization_id'))
        batch_op.drop_index('ix_services_code')
        batch_op.create_index(batch_op.f('ix_services_code'), ['code'], unique=True)
        batch_op.drop_column('organization_id')

    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_users_organization_id_organizations'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_users_organization_id'))
        batch_op.drop_column('organization_id')

    op.drop_index(op.f('ix_organizations_nom'), table_name='organizations')
    op.drop_index(op.f('ix_organizations_code'), table_name='organizations')
    op.drop_table('organizations')
