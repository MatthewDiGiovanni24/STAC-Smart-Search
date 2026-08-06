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
from dataclasses import dataclass
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

EXACT = "exact"
SEMANTIC = "semantic"
LANES = (EXACT, SEMANTIC)

# Providers queried concurrently within one lane.
CHUNK_SIZE = 10


@dataclass(frozen=True)
class WorkItem:
    """One provider search, tagged with the lane whose quota it draws from."""

    provider: Any
    adapter: GenericSTACAdapter
    request: STACSearchRequest
    lane: str


def _lane_quotas(limit: int, has_text: bool) -> dict[str, int]:
    """Split the item budget between the lanes.

    Without query text there is no lexical lane, so the whole budget is semantic.
    """
    if not has_text:
        return {EXACT: 0, SEMANTIC: limit}
    exact = limit // 2
    return {EXACT: exact, SEMANTIC: limit - exact}


def _blocked_order(items: list[NormalizedSTACItem]) -> list[NormalizedSTACItem]:
    """Order exact matches first, each block internally by relevance descending.

    Strictly blocked: the two lanes' scores are never compared. Python's sort is
    stable, so equal keys keep arrival order. Unscored items sort last within
    their own block — ``-relevance_score`` alone would rank ``None`` as 0.0 and
    interleave unscored items with genuinely low-scoring ones.
    """
    return sorted(
        items,
        key=lambda it: (
            it.properties.get("match_type") != EXACT,
            it.relevance_score is None,
            -(it.relevance_score or 0.0),
        ),
    )


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
) -> tuple[list[WorkItem], list[float] | None, dict[str, str]]:
    """Embed the query, shortlist collections, and build lane-tagged work.

    Returns ``(work, search_vector, tier_by_collection)``. Each work item is
    scoped to its lane's quota; ``tier_by_collection`` maps a collection id to
    its fine lexical tier (exact/prefix/substring/semantic) so items can be
    tagged with the tier the SQL actually assigned, not just the coarse lane.
    """
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
    tier_by_collection = {c["id"]: c.get("match_tier", SEMANTIC) for c in candidates}

    providers = await list_active_providers(pool)
    provider_lookup = {p["id"]: p for p in providers}
    quotas = _lane_quotas(request.limit, bool(request.text))

    def _build(lane: str, collections: list[dict[str, Any]]) -> list[WorkItem]:
        by_provider: dict[int, list[str]] = {}
        for c in collections:
            by_provider.setdefault(c["provider_id"], []).append(c["id"])

        built: list[WorkItem] = []
        for pid, c_ids in by_provider.items():
            if provider := provider_lookup.get(pid):  # else stale: provider inactive
                adapter = GenericSTACAdapter(base_url=provider["base_url"], source=provider["source"])
                scoped_req = request.model_copy(
                    update={"collections": c_ids, "limit": max(1, quotas[lane])}
                )
                built.append(WorkItem(provider, adapter, scoped_req, lane))
        return built

    if not request.text:
        # No text means no lexical lane; everything draws on the semantic budget.
        return _build(SEMANTIC, candidates), search_vector, tier_by_collection

    # The exact/semantic split is decided in SQL (see get_candidate_collections)
    # so the label always agrees with the ordering the query itself applied.
    exact_colls = [c for c in candidates if c.get("is_exact")]
    exact_keys = {(c["provider_id"], c["id"]) for c in exact_colls}
    semantic_colls = [c for c in candidates if (c["provider_id"], c["id"]) not in exact_keys]

    # Cap the semantic shortlist by its own item quota, not a magic number: in the
    # worst case each collection contributes one item, so this many can still fill
    # the quota. Exact matches are what the user literally typed, so they are not
    # capped here — their item budget bounds them.
    semantic_colls = semantic_colls[: max(1, quotas[SEMANTIC])]

    return _build(EXACT, exact_colls) + _build(SEMANTIC, semantic_colls), search_vector, tier_by_collection


def _tag_item(item: NormalizedSTACItem, lane: str, tier_by_collection: dict[str, str]) -> None:
    """Stamp lane + fine tier onto an item's properties.

    Keeps the coarse ``match_type`` (exact/semantic) and ``is_exact`` on the wire
    for backward compatibility, and adds the fine ``match_tier``
    (exact/prefix/substring/semantic) the SQL assigned to the item's collection.
    """
    item.properties["match_type"] = lane
    item.properties["is_exact"] = lane == EXACT
    item.properties["match_tier"] = tier_by_collection.get(item.collection or "", lane)


def _apply_lane_quotas(
    by_lane: dict[str, list[NormalizedSTACItem]], quotas: dict[str, int], limit: int
) -> list[NormalizedSTACItem]:
    """Take each lane's quota, then let either lane spill into what the other left.

    Backfill happens only after both lanes are known to be exhausted, so the
    guarantee is "at least ``min(available, quota)`` from each lane". Symmetric:
    whichever lane is short, the other fills the remainder.
    """
    kept: list[NormalizedSTACItem] = []
    leftovers: dict[str, list[NormalizedSTACItem]] = {}
    for lane in LANES:
        items = by_lane.get(lane, [])
        kept.extend(items[: quotas[lane]])
        leftovers[lane] = items[quotas[lane] :]

    # Spill in lane order so exact keeps first claim on the unused budget.
    for lane in LANES:
        if len(kept) >= limit:
            break
        kept.extend(leftovers[lane][: limit - len(kept)])
    return kept[:limit]


async def fanout_search(request: STACSearchRequest, pool: asyncpg.Pool) -> STACSearchResponse:
    """Batch path: query all shortlisted providers, rank within each lane."""
    start = time.perf_counter()
    work, search_vector, tier_by_collection = await _prepare_work(request, pool)

    results = await asyncio.gather(
        *(w.adapter.search(w.request) for w in work),
        return_exceptions=True,
    )

    sources: dict[str, str] = {}
    items: list[NormalizedSTACItem] = []
    for w, result in zip(work, results):
        # Key by provider name so the CMR child catalogs are reported
        # individually instead of collapsing into a single "cmr" entry.
        key = w.provider["name"]
        if isinstance(result, AdapterTimeout):
            sources[key] = "timeout"
            logger.warning("Timeout searching %s", key)
        elif isinstance(result, Exception):
            sources[key] = "error"
            logger.error("Error searching %s: %s", key, result)
        else:
            sources[key] = "ok"
            if request.text:
                for item in result:
                    _tag_item(item, w.lane, tier_by_collection)
            items.extend(result)

    items = await enrich_items_with_collection_context(pool, items)

    # Rerank the merged set by semantic relevance, then apply the lane budgets.
    if get_settings().ranking_enabled and search_vector is not None:
        items = await rank_items(pool, items, search_vector)

    if request.text:
        by_lane: dict[str, list[NormalizedSTACItem]] = {lane: [] for lane in LANES}
        for item in items:
            by_lane[item.properties.get("match_type", SEMANTIC)].append(item)
        items = _blocked_order(
            _apply_lane_quotas(by_lane, _lane_quotas(request.limit, True), request.limit)
        )
    else:
        items = items[: request.limit]

    return STACSearchResponse(
        sources=sources,
        items=items,
        total=len(items),
        query_time_ms=(time.perf_counter() - start) * 1000,
    )


async def _run_provider(work: WorkItem) -> tuple[WorkItem, str, list[NormalizedSTACItem]]:
    """Run one provider search, capturing status. Never raises."""
    try:
        return work, "ok", await work.adapter.search(work.request)
    except AdapterTimeout:
        logger.warning("Timeout searching %s", work.provider["name"])
        return work, "timeout", []
    except Exception as exc:  # noqa: BLE001 - a bad provider must not kill the stream
        logger.error("Error searching %s: %s", work.provider["name"], exc)
        return work, "error", []


@dataclass(frozen=True)
class _LaneDone:
    """Queue sentinel: this lane has no more provider results coming."""

    lane: str


async def _lane_producer(
    lane: str, work: list[WorkItem], queue: asyncio.Queue, stop: asyncio.Event
) -> None:
    """Query one lane's providers in chunks, pushing results as they land.

    Each lane chunks independently, so a saturated lane can never hold back the
    other one's requests the way a single shared chunk sequence did.
    """
    try:
        for i in range(0, len(work), CHUNK_SIZE):
            if stop.is_set():
                break
            tasks = [asyncio.create_task(_run_provider(w)) for w in work[i : i + CHUNK_SIZE]]
            try:
                for coro in asyncio.as_completed(tasks):
                    queue.put_nowait(await coro)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
    finally:
        # put_nowait, not await: this also runs while the task is being cancelled.
        queue.put_nowait(_LaneDone(lane))


async def stream_search(
    request: STACSearchRequest, pool: asyncpg.Pool
) -> AsyncIterator[StreamEvent]:
    """Stream provider results as they arrive, then a final ``meta`` event.

    Both lanes run concurrently, each with its own item quota, so exact matches
    can no longer consume the whole budget and starve semantic results. An item
    is yielded only while its own lane is under quota; anything beyond that is
    held back and released at the end, but only into budget the other lane
    turned out not to need.
    """
    start = time.perf_counter()
    settings = get_settings()
    work, search_vector, tier_by_collection = await _prepare_work(request, pool)
    do_rank = settings.ranking_enabled and search_vector is not None

    quotas = _lane_quotas(request.limit, bool(request.text))
    work_by_lane: dict[str, list[WorkItem]] = {lane: [] for lane in LANES}
    for w in work:
        work_by_lane[w.lane].append(w)

    sources: dict[str, str] = {}
    all_items: list[NormalizedSTACItem] = []
    counts: dict[str, int] = {lane: 0 for lane in LANES}
    # Items their own lane has no room for, held in case the other lane
    # underfills. Never released before both lanes are done.
    overflow: dict[str, list[NormalizedSTACItem]] = {lane: [] for lane in LANES}

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    producers = [
        asyncio.create_task(_lane_producer(lane, work_by_lane[lane], queue, stop))
        for lane in LANES
    ]

    try:
        open_lanes = set(LANES)
        while open_lanes:
            message = await queue.get()
            if isinstance(message, _LaneDone):
                open_lanes.discard(message.lane)
                continue

            w, status, batch = message
            sources[w.provider["name"]] = status

            if batch:
                batch = await enrich_items_with_collection_context(pool, batch)
                if do_rank:
                    await score_items(pool, batch, search_vector)

            for item in batch:
                if request.text:
                    _tag_item(item, w.lane, tier_by_collection)
                if len(all_items) >= request.limit:
                    break
                if counts[w.lane] < quotas[w.lane]:
                    counts[w.lane] += 1
                    all_items.append(item)
                    yield "item", item
                elif len(overflow[w.lane]) < request.limit:
                    overflow[w.lane].append(item)

            if len(all_items) >= request.limit:
                break
    finally:
        stop.set()
        for producer in producers:
            producer.cancel()
        await asyncio.gather(*producers, return_exceptions=True)

    # Backfill: both lanes are finished, so any remaining budget is genuinely
    # unused and either lane may claim it. Exact gets first refusal.
    for lane in LANES:
        for item in overflow[lane]:
            if len(all_items) >= request.limit:
                break
            all_items.append(item)
            yield "item", item

    if request.text:
        ordered = _blocked_order(all_items)
    else:
        ordered = sort_by_relevance(list(all_items)) if do_rank else all_items

    ranked_ids = [item.id for item in ordered if item.id is not None]

    registry_warm = (await get_registry_status(pool)).ready

    yield "meta", {
        "ranked_ids": ranked_ids,
        "sources": sources,
        "total": len(all_items),
        "query_time_ms": (time.perf_counter() - start) * 1000,
        "registry_warm": registry_warm,
    }