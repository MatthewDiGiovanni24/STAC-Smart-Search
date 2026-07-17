"""Federated fan-out with collection pre-filtering.

The query is first narrowed to a shortlist of relevant collections (spatial +
temporal overlap, then RemoteCLIP semantic ranking; see
:mod:`app.services.registry`). Only the providers that own shortlisted
collections are queried, each scoped to just those collection ids — so a
Louisiana flood query hits the handful of relevant catalogs, not all 63.
"""

import asyncio
import logging
import time
from typing import Any

import asyncpg

from app.adapters.base import AdapterTimeout
from app.adapters.stac import GenericSTACAdapter
from app.config import get_settings
from app.models.provider import list_active_providers
from app.schemas.search import STACSearchRequest, STACSearchResponse
from app.services.embeddings import embed_query
from app.services.registry import get_candidate_collections

logger = logging.getLogger(__name__)


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


async def fanout_search(request: STACSearchRequest, pool: asyncpg.Pool) -> STACSearchResponse:
    start = time.perf_counter()
    start_time, end_time = _parse_interval(request.datetime)

    # Embed the query text with RemoteCLIP (same space as stored collection
    # embeddings). Offloaded to a thread so torch inference doesn't block the loop.
    # With no text, the shortlist is spatial/temporal only (no semantic ranking).
    search_vector = await asyncio.to_thread(embed_query, request.text) if request.text else None

    candidates = await get_candidate_collections(
        pool=pool,
        bbox=request.bbox,
        start_time=start_time,
        end_time=end_time,
        search_embedding=search_vector,
        limit=get_settings().candidate_limit,
    )

    # Group shortlisted collections by their owning provider.
    provider_to_collections: dict[int, list[str]] = {}
    for c in candidates:
        provider_to_collections.setdefault(c["provider_id"], []).append(c["id"])

    providers = await list_active_providers(pool)
    provider_lookup = {p["id"]: p for p in providers}

    # One adapter per shortlisted provider, each scoped to just its collections.
    work: list[tuple[Any, GenericSTACAdapter, STACSearchRequest]] = []
    for pid, collection_ids in provider_to_collections.items():
        provider = provider_lookup.get(pid)
        if provider is None:
            continue  # stale candidate: provider no longer active
        adapter = GenericSTACAdapter(base_url=provider["base_url"], source=provider["source"])
        scoped_request = request.model_copy(update={"collections": collection_ids})
        work.append((provider, adapter, scoped_request))

    results = await asyncio.gather(
        *(adapter.search(req) for _, adapter, req in work),
        return_exceptions=True,
    )

    sources: dict[str, str] = {}
    items = []
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

    return STACSearchResponse(
        sources=sources,
        items=items,
        total=len(items),
        query_time_ms=(time.perf_counter() - start) * 1000,
    )
