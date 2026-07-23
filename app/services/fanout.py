"""Federated fan-out with collection pre-filtering.

The query is first narrowed to a shortlist of relevant collections (spatial +
temporal overlap, then RemoteCLIP semantic ranking; see
:mod:`app.services.registry`). Only the providers that own shortlisted
collections are queried, each scoped to just those collection ids — so a
Louisiana flood query hits the handful of relevant catalogs, not all 63.

Two entry points share the same setup (:func:`_prepare_work`):
  * :func:`fanout_search` — batch JSON: gather all providers, rank the full set.
  * :func:`stream_search` — SSE: yield each provider's items as it responds
    (``asyncio.as_completed``), scoring on arrival, then a final ``meta`` event
    with the authoritative global ranking.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import asyncpg

from app.adapters.base import AdapterTimeout
from app.adapters.stac import GenericSTACAdapter
from app.config import get_settings
from app.models.provider import list_active_providers
from app.schemas.search import NormalizedSTACItem, STACSearchRequest, STACSearchResponse
from app.services.embeddings import embed_query
from app.services.ranking import (
    enrich_items_with_collection_context,
    rank_items,
    score_items,
    sort_by_relevance,
)
from app.services.registry import get_candidate_collections
from app.services.registry_state import get_registry_status

logger = logging.getLogger(__name__)

# A yielded streaming event: ("item", NormalizedSTACItem) or ("meta", dict).
StreamEvent = tuple[str, Any]

def apply_exact_match_boost(items: list[NormalizedSTACItem], query: str | None) -> list[NormalizedSTACItem]:
    """Boosts exact matches to the top and updates the UI confidence score."""
    if not query:
        return items
        
    query_upper = query.upper()
    
    def is_exact_match(item: NormalizedSTACItem) -> bool:
        col_id = (item.collection or "").upper()
        item_id = (item.id or "").upper()
        return query_upper in col_id or query_upper in item_id

    # 1. Update the score for the Frontend UI card
    for item in items:
        if is_exact_match(item):
            # Update the Pydantic field for backend tracking
            if hasattr(item, "relevance_score"):
                item.relevance_score = 1.0
                
            # Update the raw properties dictionary so the UI shows 100%
            if item.properties is None:
                item.properties = {}
            item.properties["score"] = 1.0
            item.properties["relevance_score"] = 1.0

    # 2. Sort them to the top of the list
    items.sort(key=is_exact_match, reverse=True)
    return items

def _parse_interval(datetime_str: str | None) -> tuple[str | None, str | None]:
    """Split a STAC datetime value into (start, end), honoring open intervals."""
    if not datetime_str:
        return None, None
    parts = datetime_str.split("/")
    if len(parts) == 2:
        start = parts[0] if parts[0] not in ("..", "") else None
        end = parts[1] if parts[1] not in ("..", "") else None
        return start, end
    return parts[0], parts[0]


async def _prepare_work(
    request: STACSearchRequest, pool: asyncpg.Pool
) -> tuple[list[tuple[Any, GenericSTACAdapter, STACSearchRequest]], list[float] | None]:
    
    start_time, end_time = _parse_interval(request.datetime)
    search_vector = await asyncio.to_thread(embed_query, request.text) if request.text else None

    # Fetch all collections (Exact + Semantic)
    candidates = await get_candidate_collections(
        pool=pool,
        text=request.text,  
        bbox=request.bbox,
        start_time=start_time,
        end_time=end_time,
        search_embedding=search_vector,
        limit=get_settings().candidate_limit,
    )

    providers = await list_active_providers(pool)
    provider_lookup = {p["id"]: p for p in providers}
    work: list[tuple[Any, GenericSTACAdapter, STACSearchRequest]] = []

    if request.text:
        query_upper = request.text.upper()
        
        # Split into Exact Matches and Semantic Matches
        exact_colls = [c for c in candidates if query_upper in c["id"].upper() or query_upper in c.get("title", "").upper()]
        semantic_colls = [c for c in candidates if c not in exact_colls][:10]
        
        # Group EXACT matches by provider
        exact_by_prov: dict[int, list[str]] = {}
        for c in exact_colls:
            exact_by_prov.setdefault(c["provider_id"], []).append(c["id"])
            
        # Group SEMANTIC matches by provider
        semantic_by_prov: dict[int, list[str]] = {}
        for c in semantic_colls:
            semantic_by_prov.setdefault(c["provider_id"], []).append(c["id"])

        # Create dedicated requests for EXACT matches
        for pid, c_ids in exact_by_prov.items():
            if provider := provider_lookup.get(pid):
                adapter = GenericSTACAdapter(base_url=provider["base_url"], source=provider["source"])
                scoped_req = request.model_copy(update={"collections": c_ids, "limit": 75})
                work.append((provider, adapter, scoped_req))

        # Create dedicated requests for SEMANTIC matches
        for pid, c_ids in semantic_by_prov.items():
            if provider := provider_lookup.get(pid):
                adapter = GenericSTACAdapter(base_url=provider["base_url"], source=provider["source"])
                scoped_req = request.model_copy(update={"collections": c_ids, "limit": 100})
                work.append((provider, adapter, scoped_req))

    else:
        # Standard flow if no text was typed
        provider_to_collections: dict[int, list[str]] = {}
        for c in candidates:
            provider_to_collections.setdefault(c["provider_id"], []).append(c["id"])
            
        for pid, collection_ids in provider_to_collections.items():
            if provider := provider_lookup.get(pid):
                adapter = GenericSTACAdapter(base_url=provider["base_url"], source=provider["source"])
                scoped_req = request.model_copy(update={"collections": collection_ids, "limit": request.limit})
                work.append((provider, adapter, scoped_req))

    return work, search_vector


async def fanout_search(request: STACSearchRequest, pool: asyncpg.Pool) -> STACSearchResponse:
    """Batch path: query all shortlisted providers, rank the full merged set."""
    start = time.perf_counter()
    work, search_vector = await _prepare_work(request, pool)

    results = await asyncio.gather(
        *(adapter.search(req) for _, adapter, req in work),
        return_exceptions=True,
    )

    sources: dict[str, str] = {}
    items: list[NormalizedSTACItem] = []
    for (provider, _, _), result in zip(work, results):
        # Key by provider name so the CMR child catalogs are reported
        # individually instead of collapsing into a single "cmr" entry.
        key = provider["name"]
        if isinstance(result, AdapterTimeout):
            sources[key] = "timeout"
            logger.warning("Timeout searching %s", key)
        elif isinstance(result, Exception):
            sources[key] = "error"
            logger.error("Error searching %s: %s", key, result)
        else:
            sources[key] = "ok"
            items.extend(result)

    items = await enrich_items_with_collection_context(pool, items)

    # Rerank the merged set by semantic relevance, then cap to limit.
    if get_settings().ranking_enabled and search_vector is not None:
        items = await rank_items(pool, items, search_vector)
    items = items[: request.limit]

    return STACSearchResponse(
        sources=sources,
        items=items,
        total=len(items),
        query_time_ms=(time.perf_counter() - start) * 1000,
    )


async def _run_provider(
    provider: Any, adapter: GenericSTACAdapter, req: STACSearchRequest
) -> tuple[Any, str, list[NormalizedSTACItem]]:
    """Run one provider search, capturing status. Never raises."""
    try:
        return provider, "ok", await adapter.search(req)
    except AdapterTimeout:
        logger.warning("Timeout searching %s", provider["name"])
        return provider, "timeout", []
    except Exception as exc:  # noqa: BLE001 - a bad provider must not kill the stream
        logger.error("Error searching %s: %s", provider["name"], exc)
        return provider, "error", []


async def stream_search(
    request: STACSearchRequest, pool: asyncpg.Pool
) -> AsyncIterator[StreamEvent]:
    """Stream provider results as they arrive, then a final ``meta`` event.

    Uses a Progressive Fanout: queries catalogs in chunks so we don't spam APIs.
    Yields ``("item", NormalizedSTACItem)`` the moment a provider responds. 
    Stops dynamically once a target quota of items is met.
    """
    start = time.perf_counter()
    settings = get_settings()
    work, search_vector = await _prepare_work(request, pool)
    do_rank = settings.ranking_enabled and search_vector is not None

    sources: dict[str, str] = {}
    all_items: list[NormalizedSTACItem] = []
    
    # Progressive Fanout Settings
    TARGET_ITEMS = request.limit
    CHUNK_SIZE = 10

    # Process the collections in smaller chunks
    for i in range(0, len(work), CHUNK_SIZE):
        chunk = work[i : i + CHUNK_SIZE]
        
        # Fire off requests only for the current chunk
        tasks = [asyncio.create_task(_run_provider(p, a, r)) for p, a, r in chunk]
        
        try:
            for coro in asyncio.as_completed(tasks):
                provider, status, batch = await coro
                sources[provider["name"]] = status
                
                if batch:
                    batch = await enrich_items_with_collection_context(pool, batch)
                    if do_rank:
                        await score_items(pool, batch, search_vector)
                        
                for item in batch:
                    all_items.append(item)
                    yield "item", item
                    
                    # If we hit our target, stop yielding items from this batch
                    if len(all_items) >= TARGET_ITEMS:
                        break
                        
                # Break out of the task-completion loop if quota is met
                if len(all_items) >= TARGET_ITEMS:
                    break
        finally:
            # Clean up any pending tasks in this chunk if we broke out early
            for task in tasks:
                if not task.done():
                    task.cancel()
                    
        # If we hit our target, do not proceed to the next chunk
        if len(all_items) >= TARGET_ITEMS:
            break

    ordered = sort_by_relevance(list(all_items)) if do_rank else all_items
    
    ordered = apply_exact_match_boost(ordered, request.text)
    
    ranked_ids = [item.id for item in ordered if item.id is not None]

    registry_warm = (await get_registry_status(pool)).ready

    yield "meta", {
        "ranked_ids": ranked_ids,
        "sources": sources,
        "total": len(all_items),
        "query_time_ms": (time.perf_counter() - start) * 1000,
        "registry_warm": registry_warm,
    }