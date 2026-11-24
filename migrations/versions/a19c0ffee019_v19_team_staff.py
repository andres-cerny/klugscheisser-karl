"""v19: team staff appointments (coach, assistants, GM, physio …) with since/until

Revision ID: a19c0ffee019
Revises: f2b1c0ffee12
Create Date: 2026-10-19 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a19c0ffee019'
down_revision: Union[str, Sequence[str], None] = 'f2b1c0ffee12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'team_staff',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('athlete_id', sa.Integer(), nullable=False),
        sa.Column('role_key', sa.String(length=30), nullable=False),
        sa.Column('role_label', sa.Text(), nullable=True),
        sa.Column('since', sa.String(length=10), nullable=True),
        sa.Column('until', sa.String(length=10), nullable=True),
        sa.Column('last_seen', sa.Date(), nullable=True),
        sa.Column('source', sa.Text(), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('source_date', sa.Date(), nullable=True),
        sa.Column('quote', sa.Text(), nullable=True),
        sa.Column('origin', sa.String(length=20), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['athlete_id'], ['athletes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_team_staff_team_id', 'team_staff', ['team_id'])
    op.create_index('ix_team_staff_athlete_id', 'team_staff', ['athlete_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_team_staff_athlete_id', table_name='team_staff')
    op.drop_index('ix_team_staff_team_id', table_name='team_staff')
    op.drop_table('team_staff')
