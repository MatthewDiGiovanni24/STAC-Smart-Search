"""add item_embeddings cache table

Phase 5 (ML ranking). Caches per-item RemoteCLIP metadata embeddings so repeat
queries skip recomputation. Pure key-value cache keyed by item identity; a
``text_hash`` guards staleness (re-embed only when the embedded metadata text
changes). No vector index — cosine similarity is computed in-process against the
handful of items in a result set, not via a SQL scan.

Revision ID: c3d9e7f1a2b4
Revises: b2f5a1c9d4e6
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = "c3d9e7f1a2b4"
down_revision: Union[str, None] = "b2f5a1c9d4e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "item_embeddings",
        sa.Column("catalog_source", sa.Text(), nullable=False),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(512), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("catalog_source", "collection", "item_id"),
    )


def downgrade() -> None:
    op.drop_table("item_embeddings")
