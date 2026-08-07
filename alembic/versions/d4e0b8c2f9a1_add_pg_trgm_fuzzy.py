"""enable pg_trgm + GIN trigram index on collections.title for the fuzzy tier

Phase 2 follow-up (isolated). Adds the typo-tolerant lexical tier's dependency:
the ``pg_trgm`` extension and a GIN trigram index on ``lower(title)`` supporting
``word_similarity`` / the ``<%`` operator.

NOTE ON INDEX USE: the current pre-filter query keeps a non-indexable
``embedding <=> q < threshold`` term in its WHERE (an OR with the lexical terms),
which forces a sequential scan regardless — so this index is NOT leveraged by
that query shape today. It is added because it is correct and cheap, and because
the real latency fix (restructuring the query into a UNION of an index-served
lexical/fuzzy branch and a vector-ranked branch) would use it. Fuzzy runs on the
title only (ids are opaque codes where trigram similarity is noise).

Revision ID: d4e0b8c2f9a1
Revises: c3d9e7f1a2b4
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d4e0b8c2f9a1"
down_revision: Union[str, None] = "c3d9e7f1a2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_collections_title_trgm "
        "ON collections USING gin (lower(title) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_collections_title_trgm")
    # Leave the extension installed: other objects may depend on it, and dropping
    # a shared extension in a downgrade is riskier than leaving it.
