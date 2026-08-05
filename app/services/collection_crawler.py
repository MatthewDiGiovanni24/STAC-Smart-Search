"""Collection registry crawler.

Populates the ``collections`` table from every registered catalog, then embeds
each collection's description with RemoteCLIP so the search pre-filter can rank
by semantic similarity.

Two crawl paths:

* **CMR fast path** — CMR exposes ~55k collections across ~61 STAC child
  providers. Rather than paginating each child's STAC ``/collections`` (20/page,
  thousands of requests), we pull them all from CMR's native ``collections.umm_json``
  API with ``page_size=2000`` + ``CMR-Search-After`` cursoring. Each collection is
  mapped back to its **owning child provider** via ``meta.provider-id`` and its
  STAC id is reconstructed as ``{ShortName}_{Version}`` — the exact id that
  ``/stac/<provider>/search?collections=[...]`` accepts. This mapping is what makes
  the fan-out shortlist route to the correct provider URL instead of collapsing
  all CMR collections together.
* **Generic STAC path** — every non-CMR provider (Planetary Computer, Earth
  Search, …) is crawled through its standard STAC ``/collections`` endpoint.

Embedding is incremental: a description's SHA-256 is stored, and a collection is
re-embedded only when its text changes (or it has no embedding yet).
"""

import asyncio
import hashlib
import logging
from typing import Any, Optional

import asyncpg
import httpx

from app.config import get_settings
from app.models.provider import list_active_providers
from app.services.embeddings import embed_texts
from app.services.registry import (
    UpsertRow,
    fetch_existing_collection_state,
    parse_date,
    to_vector_literal,
    upsert_collections,
)

logger = logging.getLogger(__name__)

CMR_HOST = "cmr.earthdata.nasa.gov"
CMR_UMM_URL = "https://cmr.earthdata.nasa.gov/search/collections.umm_json"

# Embed in chunks so progress is visible and memory stays bounded on 55k rows.
_EMBED_CHUNK = 512

# Some catalog /collections endpoints (e.g. Planetary Computer) are large and
# intermittently slow; retry transient timeouts before giving up on a provider.
_GET_RETRIES = 3


_TRANSIENT_STATUS = {502, 503, 504}


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET, retrying transient failures (timeouts, 502/503/504). 4xx fails fast."""
    last_exc: Optional[Exception] = None
    for attempt in range(_GET_RETRIES):
        try:
            resp = await client.get(url, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.TimeoutException as exc:  # transient network timeout
            last_exc = exc
        except httpx.HTTPStatusError as exc:  # only retry transient gateway errors
            if exc.response.status_code not in _TRANSIENT_STATUS:
                raise
            last_exc = exc
        await asyncio.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


# --- raw crawl records -----------------------------------------------------


def _build_text(title: Optional[str], description: Optional[str], keywords: Any = None) -> str:
    """Compose the text to embed from title + description + keywords (capped)."""
    parts: list[str] = []
    if title:
        parts.append(str(title))
    if description:
        parts.append(str(description))
    if keywords:
        parts.append(" ".join(keywords) if isinstance(keywords, (list, tuple)) else str(keywords))
    text = ". ".join(p.strip() for p in parts if p and p.strip())
    # CLIP's text tower truncates to 77 tokens anyway; cap chars to be safe.
    return text[:1000]


def _raw(provider_id: int, cid: str, title, description, spatial, temporal, keywords=None) -> dict:
    return {
        "provider_id": provider_id,
        "id": cid,
        "title": title,
        "description": description,
        "spatial": spatial,          # [min_x, min_y, max_x, max_y] or None
        "temporal": temporal,        # [start, end] ISO strings (either may be None) or None
        "text": _build_text(title, description, keywords),
    }


# --- generic STAC /collections crawl --------------------------------------


def _stac_spatial(collection: dict) -> Optional[list[float]]:
    try:
        bbox = collection["extent"]["spatial"]["bbox"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(bbox, (list, tuple)):
        return None
    if len(bbox) >= 6:  # 3D bbox [w, s, minz, e, n, maxz]
        return [float(bbox[0]), float(bbox[1]), float(bbox[3]), float(bbox[4])]
    if len(bbox) == 4:
        return [float(x) for x in bbox]
    return None


def _stac_temporal(collection: dict) -> Optional[list]:
    try:
        interval = collection["extent"]["temporal"]["interval"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(interval, (list, tuple)) and len(interval) >= 2:
        return [interval[0], interval[1]]
    return None


async def crawl_stac_collections(
    client: httpx.AsyncClient, provider: dict, limit: Optional[int] = None
) -> list[dict]:
    """Crawl one provider's STAC ``/collections`` (paginated via rel=next)."""
    provider_id = provider["id"]
    url: Optional[str] = provider["base_url"].rstrip("/") + "/collections?limit=100"
    out: list[dict] = []
    pages = 0
    while url and pages < 200:
        resp = await _get_with_retry(client, url, headers={"Accept": "application/json"})
        data = resp.json()
        cols = data.get("collections", []) if isinstance(data, dict) else []
        for c in cols:
            out.append(
                _raw(
                    provider_id,
                    c.get("id"),
                    c.get("title"),
                    c.get("description"),
                    _stac_spatial(c),
                    _stac_temporal(c),
                    c.get("keywords"),
                )
            )
            if limit and len(out) >= limit:
                return out
        pages += 1
        url = next(
            (lk.get("href") for lk in data.get("links", []) if lk.get("rel") == "next"), None
        )
        if not cols:
            break
    return out


# --- CMR native umm_json crawl --------------------------------------------


def _cmr_provider_map(providers: list) -> dict[str, int]:
    """Map CMR child short-name -> provider row id (e.g. 'LPCLOUD' -> 42).

    Skips the virtual 'ALL' provider; real collections always carry a concrete
    provider-id, and 'ALL' would double-count.
    """
    out: dict[str, int] = {}
    for p in providers:
        if CMR_HOST in p["base_url"]:
            short = p["base_url"].rstrip("/").rsplit("/", 1)[-1]
            if short and short != "ALL":
                out[short] = p["id"]
    return out


def _cmr_bbox(umm: dict) -> Optional[list[float]]:
    try:
        rects = umm["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]["BoundingRectangles"]
    except (KeyError, TypeError):
        return None
    if not rects:
        return None
    ws, ss, es, ns = [], [], [], []
    for r in rects:
        if not isinstance(r, dict):
            continue
        if "WestBoundingCoordinate" in r:
            ws.append(r["WestBoundingCoordinate"])
        if "SouthBoundingCoordinate" in r:
            ss.append(r["SouthBoundingCoordinate"])
        if "EastBoundingCoordinate" in r:
            es.append(r["EastBoundingCoordinate"])
        if "NorthBoundingCoordinate" in r:
            ns.append(r["NorthBoundingCoordinate"])
    if not (ws and ss and es and ns):
        return None
    return [float(min(ws)), float(min(ss)), float(max(es)), float(max(ns))]


def _cmr_temporal(umm: dict) -> Optional[list]:
    starts, ends = [], []
    ongoing = False
    for te in umm.get("TemporalExtents") or []:
        for rdt in te.get("RangeDateTimes") or []:
            if rdt.get("BeginningDateTime"):
                starts.append(rdt["BeginningDateTime"])
            if rdt.get("EndingDateTime"):
                ends.append(rdt["EndingDateTime"])
            else:
                ongoing = True  # open-ended range
        for sdt in te.get("SingleDateTimes") or []:
            starts.append(sdt)
            ends.append(sdt)
    start = min(starts) if starts else None
    end = None if ongoing else (max(ends) if ends else None)
    if start is None and end is None:
        return None
    return [start, end]


def _cmr_stac_id(short_name: Optional[str], version: Any) -> Optional[str]:
    """Reconstruct the CMR-STAC collection id from ShortName + Version.

    Convention: ``{ShortName}_{Version}``, EXCEPT the CMR sentinel version
    ``"Not provided"`` (and a missing version), where CMR-STAC uses the bare
    ShortName. Returns None if there is no ShortName to build from.
    """
    if not short_name:
        return None
    if version is None or str(version).strip() == "Not provided":
        return short_name
    return f"{short_name}_{version}"


def _cmr_item_to_raw(item: dict, provider_map: dict[str, int]) -> Optional[dict]:
    """Map one native umm_json item to a raw record, or None if unroutable.

    Returns None when the collection's CMR provider isn't a registered STAC
    child (can't be routed) or it lacks a ShortName (can't build an id).
    """
    meta = item.get("meta") or {}
    umm = item.get("umm") or {}
    provider_id = provider_map.get(meta.get("provider-id"))
    if provider_id is None:
        return None
    stac_id = _cmr_stac_id(umm.get("ShortName"), umm.get("Version"))
    if not stac_id:
        return None
    return _raw(
        provider_id,
        stac_id,
        umm.get("EntryTitle"),
        umm.get("Abstract"),
        _cmr_bbox(umm),
        _cmr_temporal(umm),
    )


def _search_after_bytes(resp: httpx.Response) -> Optional[bytes]:
    """Return the raw ``CMR-Search-After`` cursor as bytes, or None.

    The cursor encodes the last collection's sort values and can contain
    non-ASCII characters (e.g. an accented EntryTitle). httpx ASCII-encodes
    *str* request-header values, so feeding the cursor back as a str crashes in
    ``_normalize_header_value`` with UnicodeEncodeError. Reading the raw response
    bytes and sending them back verbatim (httpx passes bytes header values
    through unencoded) round-trips any cursor safely.
    """
    for name, value in resp.headers.raw:
        if name.lower() == b"cmr-search-after":
            return value
    return None


async def crawl_cmr_native(
    client: httpx.AsyncClient, provider_map: dict[str, int], max_collections: Optional[int] = None
) -> tuple[list[dict], int]:
    """Crawl all CMR collections via the native umm_json API + search-after.

    Returns (rows, skipped) where ``skipped`` counts collections whose CMR
    provider isn't a registered STAC child (can't be routed, so excluded). On an
    unexpected page failure the crawl stops early and returns what it has, rather
    than losing every page collected so far.
    """
    settings = get_settings()
    out: list[dict] = []
    skipped = 0
    search_after: Optional[bytes] = None  # raw cursor bytes; sent back verbatim
    pages = 0
    while pages < 2000:
        headers: dict[str, Any] = {"Accept": "application/json"}
        if search_after:
            headers["CMR-Search-After"] = search_after  # bytes: httpx sends unencoded
        try:
            resp = await _get_with_retry(
                client,
                CMR_UMM_URL,
                params={"page_size": 2000},
                headers=headers,
                timeout=settings.discovery_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - keep the pages already collected
            logger.exception("CMR crawl stopped early at page %d; keeping %d collected", pages, len(out))
            break
        items = resp.json().get("items", [])
        if not items:
            break
        for it in items:
            raw = _cmr_item_to_raw(it, provider_map)
            if raw is None:
                skipped += 1  # unmapped CMR provider or missing ShortName
                continue
            out.append(raw)
            if max_collections and len(out) >= max_collections:
                return out, skipped
        pages += 1
        search_after = _search_after_bytes(resp)
        if not search_after:
            break
    return out, skipped


# --- orchestration ---------------------------------------------------------


async def refresh_collection_registry(
    pool: asyncpg.Pool,
    max_cmr_collections: Optional[int] = None,
    per_provider_limit: Optional[int] = None,
) -> dict:
    """Crawl every registered catalog, embed new/changed descriptions, upsert.

    Idempotent: unchanged collections keep their existing embedding. Returns a
    summary dict of what happened.
    """
    settings = get_settings()
    providers = await list_active_providers(pool)
    cmr_map = _cmr_provider_map(providers)
    non_cmr = [p for p in providers if CMR_HOST not in p["base_url"]]

    raws: list[dict] = []
    cmr_skipped = 0
    # Collection listings can be large/slow (e.g. Planetary Computer); give the
    # crawl client a generous timeout independent of the discovery timeout.
    crawl_timeout = max(60, settings.discovery_timeout_seconds)
    async with httpx.AsyncClient(timeout=crawl_timeout, follow_redirects=True) as client:
        if cmr_map:
            # Isolate the CMR crawl: a failure here must not abort the whole
            # refresh and take the other catalogs (and any partial data) down
            # with it — the non-CMR crawls below already degrade per-provider.
            try:
                cmr_rows, cmr_skipped = await crawl_cmr_native(client, cmr_map, max_cmr_collections)
                raws.extend(cmr_rows)
                logger.info(
                    "CMR native crawl: %d collections (%d unmapped skipped)", len(cmr_rows), cmr_skipped
                )
            except Exception:  # noqa: BLE001 - one catalog must not zero the registry
                logger.exception("CMR native crawl failed; continuing with other catalogs")

        results = await asyncio.gather(
            *(crawl_stac_collections(client, p, per_provider_limit) for p in non_cmr),
            return_exceptions=True,
        )
        for p, res in zip(non_cmr, results):
            if isinstance(res, Exception):
                logger.error("STAC collections crawl failed for %s: %s", p["source"], res)
                continue
            raws.extend(res)
            logger.info("STAC crawl %s: %d collections", p["source"], len(res))

    # Drop records with no id (defensive) and dedupe on (provider_id, id).
    seen: set[tuple[int, str]] = set()
    deduped: list[dict] = []
    for r in raws:
        if not r["id"]:
            continue
        key = (r["provider_id"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    # Incremental embedding: only (re)embed changed-or-new descriptions.
    existing = await fetch_existing_collection_state(pool)
    to_embed: list[dict] = []
    for r in deduped:
        text = r["text"]
        r["hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
        r["embedding_str"] = None
        prev = existing.get((r["provider_id"], r["id"]))
        unchanged = bool(prev and prev[0] == r["hash"] and prev[1])
        if text and not unchanged:
            to_embed.append(r)

    embedded = 0
    for start in range(0, len(to_embed), _EMBED_CHUNK):
        chunk = to_embed[start : start + _EMBED_CHUNK]
        vectors = await asyncio.to_thread(embed_texts, [r["text"] for r in chunk])
        for r, vec in zip(chunk, vectors):
            r["embedding_str"] = to_vector_literal(vec)
        embedded += len(chunk)
        logger.info("embedded %d/%d collection descriptions", embedded, len(to_embed))

    rows: list[UpsertRow] = []
    for r in deduped:
        sp = r["spatial"] or [None, None, None, None]
        tp = r["temporal"] or [None, None]
        rows.append(
            (
                r["id"],
                r["provider_id"],
                r["title"],
                r["description"],
                sp[0], sp[1], sp[2], sp[3],
                parse_date(tp[0]),
                parse_date(tp[1]),
                r["embedding_str"],
                r["hash"],
            )
        )
    upserted = await upsert_collections(pool, rows)

    summary = {
        "providers": len(providers),
        "collections_seen": len(deduped),
        "embedded": embedded,
        "reused": len(deduped) - len(to_embed),
        "cmr_unmapped_skipped": cmr_skipped,
        "upserted": upserted,
    }
    logger.info("collection registry refresh complete: %s", summary)
    return summary
