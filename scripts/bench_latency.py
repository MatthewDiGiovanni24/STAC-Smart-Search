"""Measure median search latency: concurrent async fan-out vs. sequential.

Baseline = querying the federated catalogs one at a time (what a naive, non-
concurrent client/implementation does). Platform = the async fan-out that issues
all catalog requests at once (asyncio). Reports median wall-clock for each and
the reduction, over the full set of discovered providers.

    DATABASE_URL=postgresql://…/db python scripts/bench_latency.py

Live measurement: hits the real catalogs, so numbers vary with network/catalog
health. The concurrent time is bounded by the slowest catalog (incl. timeouts).
"""

import asyncio
import statistics
import time

from app.database import close_db_pool, init_db_pool
from app.services.discovery import run_discovery
from app.adapters.stac import GenericSTACAdapter
from app.schemas.search import STACSearchRequest
from app.models.provider import list_active_providers

TRIALS = 3
REQUEST = STACSearchRequest(
    bbox=[-93.8, 28.9, -88.7, 33.0],
    datetime="2023-01-01T00:00:00Z/2023-12-31T23:59:59Z",
    limit=10,
)


async def _one(adapter: GenericSTACAdapter) -> bool:
    try:
        await adapter.search(REQUEST)
        return True
    except Exception:
        return False


async def main() -> None:
    pool = await init_db_pool()
    await run_discovery(pool)
    providers = await list_active_providers(pool)
    adapters = [GenericSTACAdapter(base_url=p["base_url"], source=p["source"]) for p in providers]
    print(f"Benchmarking fan-out across {len(adapters)} catalogs, {TRIALS} trials\n")

    seqs, cons = [], []
    for i in range(TRIALS):
        t = time.perf_counter()
        for a in adapters:
            await _one(a)
        seq = time.perf_counter() - t

        t = time.perf_counter()
        ok = await asyncio.gather(*(_one(a) for a in adapters))
        con = time.perf_counter() - t

        seqs.append(seq)
        cons.append(con)
        print(f"trial {i + 1}: sequential={seq:6.1f}s  concurrent={con:6.1f}s  "
              f"reduction={(1 - con / seq) * 100:4.0f}%  (catalogs ok: {sum(ok)}/{len(adapters)})")

    ms, mc = statistics.median(seqs), statistics.median(cons)
    print(f"\nMedian sequential : {ms:.1f}s")
    print(f"Median concurrent : {mc:.1f}s")
    print(f"Median latency reduction: {(1 - mc / ms) * 100:.0f}%  ({ms / mc:.1f}x faster)")

    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
