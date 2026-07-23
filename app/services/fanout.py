"""Federated fan-out with collection pre-filtering."""

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

StreamEvent = tuple[str, Any]

def _parse_interval(datetime_str: str | None) -> tuple[str | None, str | None]:
    if not datetime_str:
        return None, None
    parts = datetime_str.split("/")
    if len(parts) == 2:
        start = parts[0] if parts[0] not in ("..", "") else None
        end = parts[1] if parts[1] not in ("..", "") else None
        return start, end
    return parts[0], parts[0]


# --- EXACT MATCH BOOST HELPERS ---
def _set_exact_match_confidence(items: list[NormalizedSTACItem], query: str | None) -> list[NormalizedSTACItem]:
    """Set 100% confidence for exact-match items."""
    if not query:
        return items
    query_upper = query.upper()
    for item in items:
        col_id = (item.collection or "").upper()
        item_id = (item.id or "").upper()
        if query_upper in col_id or query_upper in item_id:
            item.relevance_score = 1.0  # Force 100% confidence
    return items

def apply_exact_match_boost(items: list[NormalizedSTACItem], query: str | None) -> list[NormalizedSTACItem]:
    """Boosts exact matches to the top and ensures 100% confidence."""
    items = _set_exact_match_confidence(items, query)
    if not query:
        return items
    query_upper = query.upper()
    # Move exact matches to the front of the list
    items.sort(key=lambda x: query_upper in (x.collection or "").upper() or query_upper in (x.id or "").upper(), reverse=True)
    return items
# ---------------------------------


async def _prepare_work(
    request: STACSearchRequest, pool: asyncpg.Pool
) -> tuple[list[tuple[Any, GenericSTACAdapter, STACSearchRequest]], list[float] | None]:
    
    start_time, end_time = _parse_interval(request.datetime)
    search_vector = await asyncio.to_thread(embed_query, request.text) if request.text else None

    # Keeping your original, un-sliced collection fetch!
    candidates = await get_candidate_collections(
        pool=pool,
        text=request.text, 
        bbox=request.bbox,
        start_time=start_time,
        end_time=end_time,
        search_embedding=search_vector,
        limit=get_settings().candidate_limit,
    )

    provider_to_collections: dict[int, list[str]] = {}
    for c in candidates:
        provider_to_collections.setdefault(c["provider_id"], []).append(c["id"])

    providers = await list_active_providers(pool)
    provider_lookup = {p["id"]: p for p in providers}

    work: list[tuple[Any, GenericSTACAdapter, STACSearchRequest]] = []
    for pid, collection_ids in provider_to_collections.items():
        if not collection_ids:
            continue
        provider = provider_lookup.get(pid)
        if provider is None:
            continue
        adapter = GenericSTACAdapter(base_url=provider["base_url"], source=provider["source"])
        scoped_request = request.model_copy(update={"collections": collection_ids})
        work.append((provider, adapter, scoped_request))

    return work, search_vector


async def fanout_search(request: STACSearchRequest, pool: asyncpg.Pool) -> STACSearchResponse:
    start = time.perf_counter()
    work, search_vector = await _prepare_work(request, pool)

    results = await asyncio.gather(
        *(adapter.search(req) for _, adapter, req in work),
        return_exceptions=True,
    )

    sources: dict[str, str] = {}
    items: list[NormalizedSTACItem] = []
    for (provider, _, _), result in zip(work, results):
        key = provider["name"]
        if isinstance(result, AdapterTimeout):
            sources[key] = "timeout"
        elif isinstance(result, Exception):
            sources[key] = "error"
        else:
            sources[key] = "ok"
            items.extend(result)

    items = await enrich_items_with_collection_context(pool, items)

    if get_settings().ranking_enabled and search_vector is not None:
        items = await rank_items(pool, items, search_vector)

    # Apply exact match boost right before slicing!
    items = apply_exact_match_boost(items, request.text)

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
    try:
        return provider, "ok", await adapter.search(req)
    except AdapterTimeout:
        return provider, "timeout", []
    except Exception as exc:
        logger.error("Error searching %s: %s", provider["name"], exc)
        return provider, "error", []


async def stream_search(
    request: STACSearchRequest, pool: asyncpg.Pool
) -> AsyncIterator[StreamEvent]:
    start = time.perf_counter()
    settings = get_settings()
    work, search_vector = await _prepare_work(request, pool)
    do_rank = settings.ranking_enabled and search_vector is not None

    sources: dict[str, str] = {}
    all_items: list[NormalizedSTACItem] = []
    
    # Keeping your exact original stream limits
    TARGET_ITEMS = 40
    CHUNK_SIZE = 10

    for i in range(0, len(work), CHUNK_SIZE):
        chunk = work[i : i + CHUNK_SIZE]
        tasks = [asyncio.create_task(_run_provider(p, a, r)) for p, a, r in chunk]
        
        try:
            for coro in asyncio.as_completed(tasks):
                provider, status, batch = await coro
                sources[provider["name"]] = status
                
                if batch:
                    batch = await enrich_items_with_collection_context(pool, batch)
                    if do_rank:
                        await score_items(pool, batch, search_vector)
                    
                    # Ensure confidence updates mid-stream
                    batch = _set_exact_match_confidence(batch, request.text)
                        
                for item in batch:
                    all_items.append(item)
                    yield "item", item
                    
                    if len(all_items) >= TARGET_ITEMS:
                        break
                        
                if len(all_items) >= TARGET_ITEMS:
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                    
        if len(all_items) >= TARGET_ITEMS:
            break

    ordered = sort_by_relevance(list(all_items)) if do_rank else all_items
    
    # Apply exact match boost right before yielding meta!
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