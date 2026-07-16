"""resize collections.embedding to 512-dim (RemoteCLIP) + registry indexes

Phase 4 fix. Two things change here:

1. The ``embedding`` column moves from ``Vector(384)`` (the earlier
   sentence-transformers seed) to ``Vector(512)`` for RemoteCLIP ViT-B/32.
   pgvector encodes dimensionality in the column type, so it CANNOT be altered
   in place — the column must be dropped and recreated. Any existing embeddings
   were produced by the wrong model on the wrong (external) dataset, so
   discarding them is intentional; the crawler re-embeds from scratch. This
   migration is written to run whether or not the table already holds seed rows:
   metadata rows are preserved, only ``embedding`` is reset to NULL.

2. Add bookkeeping columns for incremental crawl/embed (``description_hash``,
   ``last_crawled_at``) and the indexes the pre-filter/similarity query needs:
   an HNSW cosine index on ``embedding`` plus btree indexes for the
   spatial/temporal overlap filters.

Revision ID: b2f5a1c9d4e6
Revises: 3d8612babf86
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = "b2f5a1c9d4e6"
down_revision: Union[str, None] = "3d8612babf86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. Recreate the embedding column at 512 dims -----------------------
    # Drop is safe whether the column holds data or not; dependent indexes (none
    # existed on the old column) would be dropped with it. Existing metadata
    # rows are untouched — only their (invalid) embeddings are cleared.
    op.drop_column("collections", "embedding")
    op.add_column(
        "collections",
        sa.Column("embedding", Vector(512), nullable=True),
    )

    # --- 2. Incremental-crawl bookkeeping ----------------------------------
    op.add_column(
        "collections",
        sa.Column("description_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "collections",
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- 3. Indexes ---------------------------------------------------------
    # HNSW cosine index for semantic ranking (pgvector >= 0.5). NULL embeddings
    # are simply skipped, so building this before the crawl fills in rows is fine.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_collections_embedding_hnsw "
        "ON collections USING hnsw (embedding vector_cosine_ops);"
    )
    # Btree indexes supporting the bbox / datetime overlap pre-filter.
    op.create_index(
        "ix_collections_bbox",
        "collections",
        ["min_x", "max_x", "min_y", "max_y"],
    )
    op.create_index(
        "ix_collections_time",
        "collections",
        ["start_time", "end_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_collections_time", table_name="collections")
    op.drop_index("ix_collections_bbox", table_name="collections")
    op.execute("DROP INDEX IF EXISTS ix_collections_embedding_hnsw;")

    op.drop_column("collections", "last_crawled_at")
    op.drop_column("collections", "description_hash")

    op.drop_column("collections", "embedding")
    op.add_column(
        "collections",
        sa.Column("embedding", Vector(384), nullable=True),
    )
