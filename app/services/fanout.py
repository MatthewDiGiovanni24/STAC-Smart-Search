import asyncio
import logging
import time
import asyncpg

from sentence_transformers import SentenceTransformer
from app.adapters.stac import GenericSTACAdapter
from app.models.provider import list_active_providers
from app.schemas.search import STACSearchRequest, STACSearchResponse
from app.services.registry import get_candidate_collections

logger = logging.getLogger(__name__)

embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

async def fanout_search(request: STACSearchRequest, pool: asyncpg.Pool) -> STACSearchResponse:
    start = time.perf_counter()
    
    # Parse dates
    start_time, end_time = None, None
    if request.datetime:
        parts = request.datetime.split('/')
        if len(parts) == 2:
            start_time = parts[0] if parts[0] not in ('..', '') else None
            end_time = parts[1] if parts[1] not in ('..', '') else None
        else:
            start_time = end_time = parts[0]

    # vector embedding
    search_text = getattr(request, "text", None) or getattr(request, "q", None)
    search_vector = embedding_model.encode(search_text).tolist() if search_text else None

    # get most relevant collections
    candidates = await get_candidate_collections(
        pool=pool,
        bbox=request.bbox,
        start_time=start_time,
        end_time=end_time,
        search_embedding=search_vector,
        limit=10
    )

    # group by provider
    provider_to_collections = {}
    for c in candidates:
        provider_to_collections.setdefault(c["provider_id"], []).append(c["id"])

    # get providers
    providers = await list_active_providers(pool)
    provider_lookup = {p["id"]: p for p in providers}

    adapters = []
    customized_requests = []

    # build requests for providers
    for pid, collection_ids in provider_to_collections.items():
        if pid in provider_lookup:
            provider = provider_lookup[pid]
            adapter = GenericSTACAdapter(base_url=provider["base_url"], source=provider["source"])
            # ask for specific collections
            req_copy = request.model_copy(update={"collections": collection_ids})
            
            adapters.append(adapter)
            customized_requests.append(req_copy)

    # fanout
    results = await asyncio.gather(
        *(adapter.search(req) for adapter, req in zip(adapters, customized_requests)), 
        return_exceptions=True
    )
    
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
        total=len(all_items),
        query_time_ms=(time.perf_counter() - start) * 1000
    )