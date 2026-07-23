"""Collection registry data-access layer (raw asyncpg).

Holds the DML for the ``collections`` table: bulk/single upsert (with
hash-guarded, COALESCE-preserving embeddings) and the pre-filter query used at
search time. DDL lives in the Alembic migrations.
"""

import asyncpg
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import get_settings
from app.schemas.collection import CollectionMetadata


def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string (accepting a trailing 'Z') into a datetime."""
    if not date_str:
        return None
    if date_str.endswith("Z"):
        date_str = date_str[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def to_vector_literal(vector: Optional[Sequence[float]]) -> Optional[str]:
    """Render a float vector as a pgvector text literal, or None."""
    if not vector:
        return None
    return "[" + ",".join(map(str, vector)) + "]"


def parse_vector(value: Any) -> Optional[List[float]]:
    """Parse a pgvector column value into a list of floats.

    We deliberately do NOT register the pgvector asyncpg codec globally: it
    would force our ``$n::vector`` string-literal writes/queries to pass
    lists instead, breaking the existing (tested) write and pre-filter paths.
    Without the codec asyncpg returns ``vector`` columns as ``"[...]"`` strings,
    which this helper parses on read. Also tolerates list/ndarray/Vector inputs.
    """
    if value is None:
        return None
    if isinstance(value, str):
        inner = value.strip().strip("[]")
        return [float(x) for x in inner.split(",")] if inner else []
    if hasattr(value, "to_list"):  # pgvector Vector object
        return [float(x) for x in value.to_list()]
    return [float(x) for x in value]  # list / tuple / ndarray


# Shared upsert. ``embedding`` and ``description_hash`` use COALESCE so that
# passing NULL for an *unchanged* collection preserves the existing embedding
# instead of wiping it (incremental re-embedding). ``last_crawled_at`` is always
# refreshed to now().
_UPSERT_SQL = """
    INSERT INTO collections (
        id, provider_id, title, description,
        min_x, min_y, max_x, max_y,
        start_time, end_time, embedding, description_hash, last_crawled_at
    ) VALUES (
        $1, $2, $3, $4,
        $5, $6, $7, $8,
        $9, $10, $11::vector, $12, now()
    )
    ON CONFLICT (id, provider_id) DO UPDATE SET
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        min_x = EXCLUDED.min_x,
        min_y = EXCLUDED.min_y,
        max_x = EXCLUDED.max_x,
        max_y = EXCLUDED.max_y,
        start_time = EXCLUDED.start_time,
        end_time = EXCLUDED.end_time,
        embedding = COALESCE(EXCLUDED.embedding, collections.embedding),
        description_hash = COALESCE(EXCLUDED.description_hash, collections.description_hash),
        last_crawled_at = now();
"""

# Column order for a bulk-upsert row tuple (matches _UPSERT_SQL $1..$12).
UpsertRow = Tuple[
    str,               # id
    int,               # provider_id
    Optional[str],     # title
    Optional[str],     # description
    Optional[float],   # min_x
    Optional[float],   # min_y
    Optional[float],   # max_x
    Optional[float],   # max_y
    Optional[datetime],  # start_time
    Optional[datetime],  # end_time
    Optional[str],     # embedding (pgvector literal or None)
    Optional[str],     # description_hash
]


async def upsert_collection(pool: asyncpg.Pool, collection: CollectionMetadata) -> None:
    """Upsert a single collection from a :class:`CollectionMetadata`."""
    min_x = min_y = max_x = max_y = None
    if collection.spatial_extent and len(collection.spatial_extent) >= 4:
        min_x, min_y, max_x, max_y = collection.spatial_extent[:4]

    start_time = end_time = None
    if collection.temporal_extent and len(collection.temporal_extent) >= 2:
        start_time = parse_date(collection.temporal_extent[0])
        end_time = parse_date(collection.temporal_extent[1])

    row: UpsertRow = (
        collection.id,
        collection.provider_id,
        collection.title,
        collection.description,
        min_x,
        min_y,
        max_x,
        max_y,
        start_time,
        end_time,
        to_vector_literal(collection.embedding),
        None,  # description_hash: single-upsert path doesn't track it
    )
    async with pool.acquire() as conn:
        await conn.execute(_UPSERT_SQL, *row)


async def upsert_collections(
    pool: asyncpg.Pool, rows: List[UpsertRow], chunk_size: int = 1000
) -> int:
    """Bulk-upsert collection rows via executemany, in chunks. Returns the count."""
    if not rows:
        return 0
    async with pool.acquire() as conn:
        for start in range(0, len(rows), chunk_size):
            await conn.executemany(_UPSERT_SQL, rows[start : start + chunk_size])
    return len(rows)


async def fetch_existing_collection_state(
    pool: asyncpg.Pool,
) -> Dict[Tuple[int, str], Tuple[Optional[str], bool]]:
    """Return {(provider_id, id): (description_hash, embedding_present)}.

    Used by the crawler to skip re-embedding collections whose description text
    is unchanged and already has an embedding.
    """
    query = "SELECT provider_id, id, description_hash, (embedding IS NOT NULL) AS has_emb FROM collections;"
    async with pool.acquire() as conn:
        rows = await conn.fetch(query)
    return {(r["provider_id"], r["id"]): (r["description_hash"], r["has_emb"]) for r in rows}


async def count_collections(pool: asyncpg.Pool) -> Tuple[int, int]:
    """Return (total collections, collections with an embedding)."""
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM collections;")
        embedded = await conn.fetchval("SELECT count(*) FROM collections WHERE embedding IS NOT NULL;")
    return int(total), int(embedded)


async def latest_crawl_time(pool: asyncpg.Pool) -> Optional[datetime]:
    """Return the most recent ``last_crawled_at`` across all collections, or None."""
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT max(last_crawled_at) FROM collections;")


async def get_candidate_collections(
    pool: Any,
    text: Optional[str] = None,
    bbox: Optional[List[float]] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    search_embedding: Optional[List[float]] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Find collections overlapping the query, optionally ranked by similarity.

    Applies a bbox AABB-overlap filter and a temporal-interval-overlap filter
    (NULL extents are treated as "unknown" and always pass), then — when a query
    embedding is supplied — orders by cosine distance and takes the top ``limit``.
    """
    query = "SELECT provider_id, id FROM collections WHERE true"
    args: List[Any] = []

    # Spatial overlap: collection box intersects the search box.
    if bbox and len(bbox) == 4:
        search_min_x, search_min_y, search_max_x, search_max_y = bbox
        query += f"""
            AND (
                min_x IS NULL OR
                (min_x <= ${len(args)+1} AND max_x >= ${len(args)+2} AND
                 min_y <= ${len(args)+3} AND max_y >= ${len(args)+4})
            )
        """
        args.extend([search_max_x, search_min_x, search_max_y, search_min_y])

    # Temporal overlap.
    if start_time and end_time:
        parsed_start = parse_date(start_time)
        parsed_end = parse_date(end_time)
        query += f"""
            AND (start_time IS NULL OR start_time <= ${len(args)+1}::timestamptz)
            AND (end_time IS NULL OR end_time >= ${len(args)+2}::timestamptz)
        """
        args.extend([parsed_end, parsed_start])

    # Semantic & Lexical Hybrid Ranking
    if search_embedding:
        threshold = get_settings().cosine_distance_threshold
        
        if text:
            vector_idx = len(args) + 1
            text_idx = len(args) + 2
            
            # Keep items if they pass the AI threshold OR if they are an exact text match
            query += f" AND (embedding <=> ${vector_idx}::vector < {threshold} OR id ILIKE ${text_idx} OR title ILIKE ${text_idx})"
            
            # Sort exact text matches to the absolute top, then sort by AI similarity
            query += f" ORDER BY (id ILIKE ${text_idx} OR title ILIKE ${text_idx}) DESC, embedding <=> ${vector_idx}::vector ASC"
            
            args.append(to_vector_literal(search_embedding))
            args.append(f"%{text}%")
        else:
            # Fallback to pure semantic search if no text was typed
            vector_idx = len(args) + 1
            query += f" AND embedding <=> ${vector_idx}::vector < {threshold}"
            query += f" ORDER BY embedding <=> ${vector_idx}::vector ASC"
            
            args.append(to_vector_literal(search_embedding))

    query += f" LIMIT ${len(args)+1}"
    args.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [{"provider_id": row["provider_id"], "id": row["id"]} for row in rows]
