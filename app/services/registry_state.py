"""Collection-registry warmth tracking and startup warm-up.

Because the first-boot crawl+embed of ~55k CMR collections takes a while, the
API comes up before the registry is populated. During that window ``/search``
legitimately returns empty results — which must be reported as "still indexing"
rather than looking broken. This module holds:

* an in-process phase tracker (idle → warming → ready/error), and
* :func:`get_registry_status`, which combines that phase with authoritative
  DB counts, and
* :func:`warm_registry`, the startup/CLI entry point that (respecting a TTL)
  runs the crawl and moves the tracker through its phases.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

from app.config import get_settings
from app.schemas.collection import RegistryStatus
from app.services.collection_crawler import refresh_collection_registry
from app.services.registry import count_cmr_collections, count_collections, latest_crawl_time

logger = logging.getLogger(__name__)


@dataclass
class _Tracker:
    phase: str = "idle"  # idle | warming | ready | error
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None


_tracker = _Tracker()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mark_warming() -> None:
    _tracker.phase = "warming"
    _tracker.started_at = _now()
    _tracker.error = None


def mark_ready() -> None:
    _tracker.phase = "ready"
    _tracker.finished_at = _now()
    _tracker.error = None


def mark_error(message: str) -> None:
    _tracker.phase = "error"
    _tracker.finished_at = _now()
    _tracker.error = message


def phase() -> str:
    return _tracker.phase


async def get_registry_status(pool: asyncpg.Pool) -> RegistryStatus:
    """Combine the in-process phase with authoritative, LIVE DB counts.

    The counts are queried on every call (never cached at startup) so a caller
    polling ``/ready`` watches ``cmr_indexed`` climb during a background crawl.
    """
    total, embedded = await count_collections(pool)
    cmr_indexed = await count_cmr_collections(pool)
    return RegistryStatus(
        ready=embedded > 0,
        phase=_tracker.phase,
        collections_indexed=total,
        collections_embedded=embedded,
        cmr_indexed=cmr_indexed,
        error=_tracker.error,
        last_refresh=_tracker.finished_at.isoformat() if _tracker.finished_at else None,
    )


async def warm_registry(
    pool: asyncpg.Pool,
    *,
    refresh_on_startup: Optional[bool] = None,
    ttl_hours: Optional[int] = None,
    max_cmr_collections: Optional[int] = None,
    per_provider_limit: Optional[int] = None,
) -> None:
    """Warm the collection registry, honoring the refresh flag and TTL.

    Skips the crawl (marking ready) when refresh is disabled, or when the
    registry already holds embeddings that were refreshed within the TTL.
    Designed to run as a background task; safe to cancel.
    """
    settings = get_settings()
    if refresh_on_startup is None:
        refresh_on_startup = settings.registry_refresh_on_startup
    if ttl_hours is None:
        ttl_hours = settings.registry_ttl_hours
    if max_cmr_collections is None:
        max_cmr_collections = settings.max_cmr_collections

    total, embedded = await count_collections(pool)
    last = await latest_crawl_time(pool)
    is_fresh = bool(embedded and last and (_now() - last) < timedelta(hours=ttl_hours))

    if not refresh_on_startup or is_fresh:
        reason = "refresh_disabled" if not refresh_on_startup else "within_ttl"
        logger.info(
            "Skipping registry crawl (%s): %d collections, %d embedded", reason, total, embedded
        )
        mark_ready() if embedded else None
        return

    mark_warming()
    logger.info("Warming collection registry (background crawl starting)...")
    try:
        summary = await refresh_collection_registry(
            pool, max_cmr_collections=max_cmr_collections, per_provider_limit=per_provider_limit
        )
        mark_ready()
        logger.info("Registry warm-up complete: %s", summary)
    except asyncio.CancelledError:
        logger.warning("Registry warm-up cancelled (shutdown)")
        raise
    except Exception as exc:  # noqa: BLE001 - startup must survive a crawl failure
        logger.exception("Registry warm-up failed")
        mark_error(str(exc))
