"""la personne rencontrée devient facultative

Beaucoup de visites ne visent personne en particulier : dépôt de dossier, retrait
de document, livraison. Le visiteur va au **service**, pas à quelqu'un. Obliger à
désigner une personne conduisait l'agent d'accueil à en choisir une au hasard —
une donnée fausse mais crédible, qui faussait le classement des agents les plus
visités du dashboard (voir ADR-019).

`visits.agent_id` passe donc en nullable. La clé étrangère et son `RESTRICT` sont
conservés : un agent référencé par une visite reste indéboulonnable.

Aucune reprise de données : les visites existantes gardent leur agent.

Revision ID: c9a4d13f6e27
Revises: b4e2af8c1d93
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9a4d13f6e27'
down_revision: Union[str, Sequence[str], None] = 'b4e2af8c1d93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    `batch_alter_table` : SQLite ne sait pas modifier une colonne en place, il faut
    recréer la table. Sur PostgreSQL, Alembic émet un simple `ALTER COLUMN`.
    """
    with op.batch_alter_table('visits') as batch_op:
        batch_op.alter_column('agent_id', existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    """Downgrade schema.

    Les visites sans personne rencontrée violeraient la contrainte restaurée : on
    les rattache d'office à un agent du service visité, faute de quoi le downgrade
    échouerait sur une base ayant déjà servi.
    """
    op.execute(
        'UPDATE visits SET agent_id = ('
        '  SELECT a.id FROM agents a WHERE a.service_id = visits.service_id LIMIT 1'
        ') WHERE agent_id IS NULL'
    )
    with op.batch_alter_table('visits') as batch_op:
        batch_op.alter_column('agent_id', existing_type=sa.Uuid(), nullable=False)
