
import asyncio
import logging
import time

import asyncpg

from app.adapters.stac import GenericSTACAdapter
from app.models.provider import list_active_providers
from app.schemas.search import STACSearchRequest, STACSearchResponse

logger = logging.getLogger(__name__)


async def fanout_search(request: STACSearchRequest, pool: asyncpg.Pool) -> STACSearchResponse:
    start = time.perf_counter()
    providers = await list_active_providers(pool)
    adapters = [
        GenericSTACAdapter(base_url=provider.base_url, source=provider.name)
        for provider in providers
        ]
    results = await asyncio.gather(*(adapter.search(request) for adapter in adapters), return_exceptions=True)
    
    sources = {}
    all_items = []
    for adapter, result in zip(adapters, results):
        if isinstance(result, Exception):
            logger.error(f"Error searching {adapter.source}: {result}")
            sources[adapter.source] = "error"
        else:
            sources[adapter.source] = "ok"
            all_items.extend(result)  

    return STACSearchResponse(
        sources=sources, 
        items=all_items,
        total = len(all_items),
        query_time_ms = (time.perf_counter() - start) * 1000

    )  