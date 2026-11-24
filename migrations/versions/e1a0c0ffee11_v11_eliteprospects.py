"""v11 elite prospects: season stats columns, ep_profiles, source_state

Revision ID: e1a0c0ffee11
Revises: d931ec0654c7
Create Date: 2026-10-04 21:30:00

"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy  # noqa: F401
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e1a0c0ffee11'
down_revision: Union[str, Sequence[str], None] = 'd931ec0654c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INT_COLS = ["plus_minus", "wins", "losses", "ties", "shutouts", "goals_against", "saves"]


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('season_stats', sa.Column('team_kind', sa.String(length=20), server_default='club', nullable=False))
    op.add_column('season_stats', sa.Column('phase', sa.String(length=10), server_default='regular', nullable=False))
    for c in INT_COLS:
        op.add_column('season_stats', sa.Column(c, sa.Integer(), nullable=True))
    op.add_column('season_stats', sa.Column('toi', sa.String(length=20), nullable=True))
    op.add_column('season_stats', sa.Column('source', sa.String(length=30), server_default='league', nullable=False))
    op.drop_constraint('season_stats_athlete_id_season_league_team_name_key', 'season_stats', type_='unique')
    op.create_unique_constraint(
        'uq_season_stats_row', 'season_stats', ['athlete_id', 'season', 'league', 'team_name', 'phase', 'source']
    )
    op.create_table('ep_profiles',
    sa.Column('athlete_id', sa.Integer(), nullable=False),
    sa.Column('ep_id', sa.String(length=20), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('raw', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('imported_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['athlete_id'], ['athletes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('athlete_id')
    )
    op.create_table('source_state',
    sa.Column('source', sa.String(length=30), nullable=False),
    sa.Column('blocked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('state', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('source')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('source_state')
    op.drop_table('ep_profiles')
    op.drop_constraint('uq_season_stats_row', 'season_stats', type_='unique')
    op.create_unique_constraint(
        'season_stats_athlete_id_season_league_team_name_key', 'season_stats',
        ['athlete_id', 'season', 'league', 'team_name'],
    )
    for c in ['source', 'toi', *INT_COLS, 'phase', 'team_kind']:
        op.drop_column('season_stats', c)
