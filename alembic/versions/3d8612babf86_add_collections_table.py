"""add collections table

Revision ID: 3d8612babf86
Revises: 0001_initial
Create Date: 2026-07-16 10:26:42.121995

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = '3d8612babf86'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'collections',
        sa.Column('id', sa.String(), nullable=False, primary_key=True),
        sa.Column('provider_id', sa.Integer(), sa.ForeignKey('providers.id', ondelete='CASCADE'), nullable=False, primary_key=True),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('min_x', sa.Float(), nullable=True),
        sa.Column('min_y', sa.Float(), nullable=True),
        sa.Column('max_x', sa.Float(), nullable=True),
        sa.Column('max_y', sa.Float(), nullable=True),
        sa.Column('start_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('embedding', Vector(384), nullable=True)
    )


def downgrade() -> None:
    op.drop_table('collections')
