"""source columns as text

Revision ID: fcb09542d828
Revises: bb2a7842b0a2
Create Date: 2026-10-03 08:17:06.835176

"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy  # noqa: F401
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fcb09542d828'
down_revision: Union[str, Sequence[str], None] = 'bb2a7842b0a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("athlete_affiliations", "source", type_=sa.Text(), existing_type=sa.String(50))
    op.alter_column("event_entries", "source", type_=sa.Text(), existing_type=sa.String(50))


def downgrade() -> None:
    op.alter_column("event_entries", "source", type_=sa.String(50), existing_type=sa.Text())
    op.alter_column("athlete_affiliations", "source", type_=sa.String(50), existing_type=sa.Text())
