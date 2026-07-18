"""Phase 5 acceptance demo: real RemoteCLIP + local Postgres, no external APIs.

Proves: (1) results come back sorted by semantic relevance, not arbitrary catalog
order; (2) repeat queries hit the pgvector item-embedding cache and skip
recomputation.
"""

import asyncio

from app.database import init_db_pool, close_db_pool
from app.schemas.search import NormalizedSTACItem
from app.services import ranking
from app.services.embeddings import embed_query, embed_texts as real_embed_texts

# Wrap the REAL embedder with a counter (still runs RemoteCLIP; just counts).
_calls = {"invocations": 0, "texts": 0}


def counting_embed(texts):
    _calls["invocations"] += 1
    _calls["texts"] += len(texts)
    return real_embed_texts(texts)


ranking.embed_texts = counting_embed

COLLECTIONS = [
    ("flood_optical", "Flood Inundation Mapping",
     "Surface water extent and flood inundation mapping from optical satellite imagery"),
    ("seaice_pm", "Arctic Sea Ice Concentration",
     "Sea ice concentration and extent in the Arctic from passive microwave sensors"),
    ("landcover", "Global Land Cover",
     "Annual global land cover classification"),
]


def build_items():
    # Deliberately arbitrary (non-relevance) catalog order.
    return [
        NormalizedSTACItem(id="ice-1", collection="seaice_pm", catalog_source="cmr", datetime="2023-08-06T00:00:00Z"),
        NormalizedSTACItem(id="flood-1", collection="flood_optical", catalog_source="cmr", datetime="2023-08-30T00:00:00Z"),
        NormalizedSTACItem(id="land-1", collection="landcover", catalog_source="cmr", datetime="2023-01-01T00:00:00Z"),
        NormalizedSTACItem(id="flood-2", collection="flood_optical", catalog_source="cmr", datetime="2023-09-02T00:00:00Z"),
        NormalizedSTACItem(id="ice-2", collection="seaice_pm", catalog_source="cmr", datetime="2023-08-10T00:00:00Z"),
    ]


async def main():
    pool = await init_db_pool()
    async with pool.acquire() as c:
        pid = await c.fetchval("INSERT INTO providers (name,base_url,source) VALUES ('cmr-test','https://x/stac/X','cmr') RETURNING id")
        for cid, title, desc in COLLECTIONS:
            await c.execute(
                "INSERT INTO collections (id,provider_id,title,description) VALUES ($1,$2,$3,$4)",
                cid, pid, title, desc,
            )

    query = "flooding and inundation after a hurricane"
    qvec = await asyncio.to_thread(embed_query, query)

    items = build_items()
    print("Query:", query)
    print("Arbitrary catalog order in:", [it.id for it in items])

    ranked = await ranking.rank_items(pool, items, qvec)
    print("\nRanked by relevance:")
    for it in ranked:
        print(f"  {it.id:8s} ({it.collection:13s}) score={it.relevance_score:.4f}")

    after_first = _calls["invocations"]
    print(f"\nembed invocations after 1st query: {after_first} (texts embedded: {_calls['texts']})")

    # Repeat the SAME query with the SAME items -> should hit the cache.
    ranked2 = await ranking.rank_items(pool, build_items(), qvec)
    after_second = _calls["invocations"]
    print(f"embed invocations after 2nd query: {after_second}")

    cache_rows = await pool.fetchval("SELECT count(*) FROM item_embeddings")

    # --- assertions ---
    flood_ranks = [i for i, it in enumerate(ranked) if it.collection == "flood_optical"]
    ice_ranks = [i for i, it in enumerate(ranked) if it.collection == "seaice_pm"]
    assert max(flood_ranks) < min(ice_ranks), "flood items must outrank sea-ice items"
    assert ranked[0].collection == "flood_optical", "top result must be flood-relevant"
    assert after_second == after_first, "2nd query must skip embedding (cache hit)"
    assert cache_rows == 5, f"expected 5 cached item embeddings, got {cache_rows}"
    assert [it.id for it in ranked2] == [it.id for it in ranked], "repeat query order must be stable"

    print("\nPASS:")
    print("  - flood items ranked above sea-ice items (semantic order, not catalog order)")
    print(f"  - 2nd identical query embedded 0 new texts (cache hit); {cache_rows} rows cached in pgvector")

    await close_db_pool()


asyncio.run(main())
