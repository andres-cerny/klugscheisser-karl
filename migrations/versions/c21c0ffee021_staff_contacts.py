"""staff contacts: work email + phone on team_staff (media managers / press officers for Fabi)

Revision ID: c21c0ffee021
Revises: b20c0ffee020
Create Date: 2026-11-01

Two nullable columns only; downgrade drops them. Filled from `seeds/staff/*.yaml` (`email`, `phone`), only with
contacts the federation/IIHF publishes itself.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c21c0ffee021"
down_revision: Union[str, Sequence[str], None] = "b20c0ffee020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("team_staff", sa.Column("email", sa.Text(), nullable=True))
    op.add_column("team_staff", sa.Column("phone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("team_staff", "phone")
    op.drop_column("team_staff", "email")
