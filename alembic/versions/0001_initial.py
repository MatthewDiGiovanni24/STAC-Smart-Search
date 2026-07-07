"""Initial schema: pgvector extension + provider registry.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pgvector is used by the ranking stage in later phases; install it now so
    # the extension is available cluster-wide from the first migration.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "providers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False, unique=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("spatial_extent", postgresql.JSONB(), nullable=True),
        sa.Column("temporal_extent", postgresql.JSONB(), nullable=True),
        sa.Column(
            "last_discovered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.create_index("ix_providers_source", "providers", ["source"])


def downgrade() -> None:
    op.drop_index("ix_providers_source", table_name="providers")
    op.drop_table("providers")
    op.execute("DROP EXTENSION IF EXISTS vector")
