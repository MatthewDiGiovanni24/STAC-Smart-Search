"""Phase 4 acceptance: a Louisiana flood query shortlists relevant collections,
not Arctic sea-ice data.

Exercises the real ``get_candidate_collections`` pre-filter (spatial + temporal
overlap, then pgvector cosine ordering) against a live Postgres. Uses
deterministic hand-crafted embeddings so it needs no ML model or network — only
a migrated database pointed at by DATABASE_URL.

    createdb stac_acc && DATABASE_URL=postgresql://…/stac_acc alembic upgrade head
    DATABASE_URL=postgresql://…/stac_acc python scripts/acceptance_prefilter.py
"""

import asyncio

from app.database import close_db_pool, init_db_pool
from app.services.registry import get_candidate_collections, to_vector_literal

DIM = 512


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


# axis 0 = "flood" direction, axis 1 = "sea ice", axis 2 = "land cover"
FLOOD, ICE, LAND = _unit(0), _unit(1), _unit(2)

COLLECTIONS = [
    # id,              bbox [minx,miny,maxx,maxy],          embedding
    ("flood_la",       [-94.0, 29.0, -89.0, 33.0],          FLOOD),   # Louisiana
    ("seaice_arctic",  [-180.0, 66.0, 180.0, 90.0],         ICE),     # Arctic (no LA overlap)
    ("landcover_glob", [-180.0, -90.0, 180.0, 90.0],        LAND),    # global (overlaps LA)
]


async def main() -> None:
    pool = await init_db_pool()
    async with pool.acquire() as c:
        pid = await c.fetchval(
            "INSERT INTO providers (name, base_url, source) VALUES ('acc','https://x/stac/X','cmr') RETURNING id"
        )
        for cid, bbox, emb in COLLECTIONS:
            await c.execute(
                """INSERT INTO collections
                   (id, provider_id, title, min_x, min_y, max_x, max_y, start_time, end_time, embedding)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,'2020-01-01Z','2024-01-01Z',$8::vector)""",
                cid, pid, cid, bbox[0], bbox[1], bbox[2], bbox[3], to_vector_literal(emb),
            )

    # Louisiana flood query: LA bbox + "flood"-direction query embedding.
    la_bbox = [-93.5, 29.5, -90.5, 31.0]
    candidates = await get_candidate_collections(
        pool=pool,
        bbox=la_bbox,
        start_time="2023-06-01T00:00:00Z",
        end_time="2023-09-01T00:00:00Z",
        search_embedding=FLOOD,
        limit=10,
    )
    ids = [c["id"] for c in candidates]
    print("Louisiana flood query shortlist (ranked):", ids)

    assert "seaice_arctic" not in ids, "Arctic sea-ice must be excluded (bbox non-overlap)"
    assert "flood_la" in ids, "Louisiana flood collection must be shortlisted"
    assert ids[0] == "flood_la", "flood collection must rank first (semantic relevance)"

    print("\nPASS:")
    print("  - Arctic sea-ice excluded by spatial filter (not fanned out to)")
    print("  - Louisiana flood collection shortlisted and ranked #1 by relevance")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
