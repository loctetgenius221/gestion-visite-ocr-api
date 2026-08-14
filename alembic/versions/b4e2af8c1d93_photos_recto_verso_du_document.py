"""photos recto et verso du document d'identité

Le scan MRZ ne capture qu'une face — le verso d'une CNI sénégalaise. Quand l'OCR
échoue et que l'agent saisit l'identité à la main, il doit pouvoir photographier
les **deux** faces : le recto porte la photo et des mentions absentes du verso
(voir ADR-018).

`mrz_image_url` désigne exactement la même face que `document_verso_url` : les
valeurs existantes y sont recopiées. La colonne est conservée le temps que l'app
mobile bascule — la retirer maintenant casserait les tablettes déployées.

Revision ID: b4e2af8c1d93
Revises: a3d7e91c40b2
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e2af8c1d93'
down_revision: Union[str, Sequence[str], None] = 'a3d7e91c40b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('visitors', sa.Column('document_recto_url', sa.String(length=500), nullable=True))
    op.add_column('visitors', sa.Column('document_verso_url', sa.String(length=500), nullable=True))
    # Reprise des images déjà scannées : elles sont toutes des versos.
    op.execute(
        'UPDATE visitors SET document_verso_url = mrz_image_url '
        'WHERE mrz_image_url IS NOT NULL'
    )


def downgrade() -> None:
    """Downgrade schema.

    `mrz_image_url` n'est pas restaurée depuis `document_verso_url` : elle n'a
    jamais été vidée à l'aller, sa valeur est donc toujours en place.
    """
    op.drop_column('visitors', 'document_verso_url')
    op.drop_column('visitors', 'document_recto_url')
