"""v12: Elite Prospects team id on season stats and career stints

Revision ID: f2b1c0ffee12
Revises: e1a0c0ffee11
Create Date: 2026-10-05 08:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b1c0ffee12'
down_revision: Union[str, Sequence[str], None] = 'e1a0c0ffee11'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('season_stats', sa.Column('ep_team_id', sa.String(length=20), nullable=True))
    op.add_column('career_stints', sa.Column('ep_team_id', sa.String(length=20), nullable=True))
    op.create_index('ix_career_stints_ep_team_id', 'career_stints', ['ep_team_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_career_stints_ep_team_id', table_name='career_stints')
    op.drop_column('career_stints', 'ep_team_id')
    op.drop_column('season_stats', 'ep_team_id')
