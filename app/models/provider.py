"""Provider registry data-access layer (raw asyncpg).

The ``providers`` table is created and versioned by Alembic (see
``alembic/versions``). This module contains the async query helpers the
application uses at runtime. Keeping DDL in migrations and DML here preserves a
clean separation between schema management and application queries.
"""

import json
from typing import Any

import asyncpg

TABLE_NAME = "providers"


async def upsert_provider(
    pool: asyncpg.Pool,
    *,
    name: str,
    base_url: str,
    source: str,
    spatial_extent: dict[str, Any] | None = None,
    temporal_extent: dict[str, Any] | None = None,
    is_active: bool = True,
) -> asyncpg.Record:
    """Insert or update a provider, keyed on ``base_url``.

    Discovery is idempotent: re-running it updates the existing row (name,
    source, extents, ``last_discovered_at``) rather than creating a duplicate.

    Returns:
        The inserted/updated row.
    """
    query = f"""
        INSERT INTO {TABLE_NAME}
            (name, base_url, source, spatial_extent, temporal_extent,
             last_discovered_at, is_active)
        VALUES ($1, $2, $3, $4, $5, now(), $6)
        ON CONFLICT (base_url) DO UPDATE SET
            name = EXCLUDED.name,
            source = EXCLUDED.source,
            spatial_extent = EXCLUDED.spatial_extent,
            temporal_extent = EXCLUDED.temporal_extent,
            last_discovered_at = now(),
            is_active = EXCLUDED.is_active
        RETURNING *;
    """
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            query,
            name,
            base_url,
            source,
            json.dumps(spatial_extent) if spatial_extent is not None else None,
            json.dumps(temporal_extent) if temporal_extent is not None else None,
            is_active,
        )


async def list_active_providers(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Return all active providers ordered by source then name."""
    query = f"""
        SELECT *
        FROM {TABLE_NAME}
        WHERE is_active = true
        ORDER BY source, name;
    """
    async with pool.acquire() as conn:
        return await conn.fetch(query)


def record_to_dict(record: asyncpg.Record) -> dict[str, Any]:
    """Convert an asyncpg row into a plain dict, decoding JSONB text columns.

    asyncpg returns ``jsonb`` columns as their raw JSON *text* unless a codec is
    registered, so decode ``spatial_extent`` / ``temporal_extent`` here.
    """
    data = dict(record)
    for key in ("spatial_extent", "temporal_extent"):
        value = data.get(key)
        if isinstance(value, str):
            data[key] = json.loads(value)
    return data
