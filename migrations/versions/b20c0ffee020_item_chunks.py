"""item_chunks: passage vectors (gemini-embedding-2 @768 halfvec) for RETRIEVAL_CHUNKS

Revision ID: b20c0ffee020
Revises: a19c0ffee019
Create Date: 2026-10-25

Adds one table only; downgrade drops it. Nothing else reads it unless RETRIEVAL_CHUNKS=true.
"""

from typing import Sequence, Union

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "b20c0ffee020"
down_revision: Union[str, Sequence[str], None] = "a19c0ffee019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "item_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("vector", pgvector.sqlalchemy.HALFVEC(768), nullable=False),
        sa.UniqueConstraint("item_id", "model", "ord"),
    )
    op.create_index("ix_item_chunks_item_id", "item_chunks", ["item_id"])
    op.create_index(
        "ix_item_chunks_hnsw",
        "item_chunks",
        ["vector"],
        postgresql_using="hnsw",
        postgresql_ops={"vector": "halfvec_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_item_chunks_hnsw", table_name="item_chunks")
    op.drop_index("ix_item_chunks_item_id", table_name="item_chunks")
    op.drop_table("item_chunks")
