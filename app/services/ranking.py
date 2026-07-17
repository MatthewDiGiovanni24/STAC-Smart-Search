"""Item-level semantic reranking.

After fan-out merges items from the shortlisted catalogs, this reranks the full
set by cosine similarity of each item's metadata embedding to the query
embedding. Item embeddings are cached in pgvector (``item_embeddings``) keyed by
item identity + a hash of the embedded text, so repeat queries skip
recomputation.

Item metadata alone is sparse (mostly ids/dates), so the text embedded for each
item is enriched with its collection's title/description from the Phase-4
registry. ``_item_text`` is intentionally deterministic — fixed field order,
only named keys (never dict iteration), and sorted list values — so the same
item always hashes identically and cache hits are not missed.
"""

import asyncio
import hashlib
import logging
from typing import Optional

import asyncpg
import numpy as np

from app.schemas.search import NormalizedSTACItem
from app.services.embeddings import embed_texts
from app.services.registry import parse_vector, to_vector_literal

logger = logging.getLogger(__name__)

# Max chars fed to the embedder (RemoteCLIP truncates to 77 tokens anyway).
_TEXT_CAP = 1000


def _item_text(item: NormalizedSTACItem, collection_context: dict[str, tuple]) -> str:
    """Build a deterministic text representation of an item's metadata.

    Determinism is essential for the ``text_hash`` cache guard: the same item
    must always produce the exact same string. We use a FIXED field order, pull
    only NAMED keys (never iterate the properties dict), and SORT any list value.
    """
    ctx_title, ctx_desc = collection_context.get(item.collection, (None, None))
    props = item.properties if isinstance(item.properties, dict) else {}

    instruments = item.instruments
    instr_str = " ".join(sorted(str(i) for i in instruments)) if instruments else None

    prop_title = props.get("title")
    prop_desc = props.get("description")

    # Fixed order; collection context first (it carries the semantic weight).
    fields = [
        ctx_title,
        ctx_desc,
        item.platform,
        instr_str,
        item.constellation,
        item.datetime,
        prop_title if isinstance(prop_title, str) else None,
        prop_desc if isinstance(prop_desc, str) else None,
    ]
    text = ". ".join(f.strip() for f in fields if isinstance(f, str) and f.strip())
    return text[:_TEXT_CAP]


def _cache_key(item: NormalizedSTACItem) -> Optional[tuple[str, str, str]]:
    """Return the cache key (source, collection, id), or None if uncacheable."""
    if item.catalog_source and item.collection and item.id:
        return (item.catalog_source, item.collection, item.id)
    return None


# --- cache / context data access -------------------------------------------


async def fetch_collection_context(
    pool: asyncpg.Pool, collection_ids: list[str]
) -> dict[str, tuple]:
    """Return {collection_id: (title, description)} for enrichment."""
    if not collection_ids:
        return {}
    query = "SELECT id, title, description FROM collections WHERE id = ANY($1::text[]);"
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, collection_ids)
    return {r["id"]: (r["title"], r["description"]) for r in rows}


async def fetch_item_embeddings(
    pool: asyncpg.Pool, keys: list[tuple[str, str, str]]
) -> dict[tuple[str, str, str], tuple[Optional[str], object]]:
    """Batch-fetch cached embeddings. Returns {key: (text_hash, embedding_raw)}."""
    if not keys:
        return {}
    sources, collections, ids = zip(*keys)
    query = """
        SELECT catalog_source, collection, item_id, text_hash, embedding
        FROM item_embeddings
        WHERE (catalog_source, collection, item_id) IN (
            SELECT * FROM unnest($1::text[], $2::text[], $3::text[])
        );
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, list(sources), list(collections), list(ids))
    return {
        (r["catalog_source"], r["collection"], r["item_id"]): (r["text_hash"], r["embedding"])
        for r in rows
    }


async def upsert_item_embeddings(pool: asyncpg.Pool, rows: list[tuple]) -> None:
    """Upsert (catalog_source, collection, item_id, text_hash, embedding_literal)."""
    if not rows:
        return
    query = """
        INSERT INTO item_embeddings (catalog_source, collection, item_id, text_hash, embedding, updated_at)
        VALUES ($1, $2, $3, $4, $5::vector, now())
        ON CONFLICT (catalog_source, collection, item_id) DO UPDATE SET
            text_hash = EXCLUDED.text_hash,
            embedding = EXCLUDED.embedding,
            updated_at = now();
    """
    async with pool.acquire() as conn:
        await conn.executemany(query, rows)


# --- ranking ----------------------------------------------------------------


async def rank_items(
    pool: asyncpg.Pool,
    items: list[NormalizedSTACItem],
    query_vector: Optional[list[float]],
) -> list[NormalizedSTACItem]:
    """Rerank items by cosine similarity to the query, setting relevance_score.

    No-ops (returns items unchanged) when there is no query vector or no items.
    Items with no usable metadata text get ``relevance_score=None`` and sort last.
    """
    if not items or not query_vector:
        return items

    # Enrich each item's text with its collection's title/description.
    collection_ids = sorted({it.collection for it in items if it.collection})
    context = await fetch_collection_context(pool, collection_ids)

    texts = [_item_text(it, context) for it in items]
    hashes = [
        hashlib.sha256(t.encode("utf-8")).hexdigest() if t else None for t in texts
    ]
    keys = [_cache_key(it) for it in items]

    cached = await fetch_item_embeddings(pool, [k for k in keys if k])

    vectors: list[Optional[list[float]]] = [None] * len(items)
    to_embed: list[int] = []
    for i in range(len(items)):
        if not texts[i]:
            continue
        key, text_hash = keys[i], hashes[i]
        hit = cached.get(key) if key else None
        if hit and hit[0] == text_hash and hit[1] is not None:
            vectors[i] = parse_vector(hit[1])  # cache hit — skip recomputation
        else:
            to_embed.append(i)

    # Embed cache misses in one batched call (offloaded off the event loop).
    if to_embed:
        fresh = await asyncio.to_thread(embed_texts, [texts[i] for i in to_embed])
        upserts = []
        for j, i in enumerate(to_embed):
            vectors[i] = fresh[j]
            if keys[i]:
                upserts.append((keys[i][0], keys[i][1], keys[i][2], hashes[i], to_vector_literal(fresh[j])))
        await upsert_item_embeddings(pool, upserts)
        logger.debug("ranking: %d cache hits, %d embedded", len(items) - len(to_embed), len(to_embed))

    # Cosine similarity == dot product (all vectors are L2-normalized).
    query = np.asarray(query_vector, dtype=float)
    for i, item in enumerate(items):
        vec = vectors[i]
        item.relevance_score = float(np.dot(query, np.asarray(vec, dtype=float))) if vec is not None else None

    # Highest score first; unscored items last (stable).
    items.sort(
        key=lambda it: (it.relevance_score is not None, it.relevance_score or 0.0),
        reverse=True,
    )
    return items
