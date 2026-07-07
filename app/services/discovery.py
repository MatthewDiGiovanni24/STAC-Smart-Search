"""Provider discovery service.

On startup this module:
  1. Fetches the NASA CMR-STAC root and registers every ``rel="child"`` link as
     a provider (source ``"cmr"``).
  2. Seeds the static providers that are not discoverable via CMR — Microsoft
     Planetary Computer and AWS Earth Search.

Both steps upsert on ``base_url``, so discovery is idempotent and safe to run on
every startup. A CMR failure is logged but does not abort seeding of the static
providers, so the service always comes up with a usable registry.
"""

import logging
from urllib.parse import urljoin

import aiohttp
import asyncpg

from app.config import get_settings
from app.models.provider import upsert_provider

logger = logging.getLogger(__name__)

# Static catalogs not discoverable through CMR's link graph.
STATIC_PROVIDERS: list[dict[str, str]] = [
    {
        "name": "Microsoft Planetary Computer",
        "base_url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "source": "planetary_computer",
    },
    {
        "name": "AWS Earth Search",
        "base_url": "https://earth-search.aws.element84.com/v1",
        "source": "earth_search",
    },
]


async def discover_cmr_providers(pool: asyncpg.Pool) -> int:
    """Fetch the CMR-STAC root and upsert each child provider.

    Returns:
        The number of CMR providers discovered and upserted.
    """
    settings = get_settings()
    root_url = settings.cmr_stac_root
    timeout = aiohttp.ClientTimeout(total=settings.discovery_timeout_seconds)

    logger.info("Discovering CMR providers from %s", root_url)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(root_url, headers={"Accept": "application/json"}) as resp:
            resp.raise_for_status()
            payload = await resp.json()

    links = payload.get("links", []) if isinstance(payload, dict) else []
    count = 0
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "child":
            continue
        href = link.get("href")
        if not href:
            continue
        # CMR hrefs are usually absolute; urljoin is a safety net for relatives.
        base_url = urljoin(root_url, href)
        name = link.get("title") or _name_from_href(base_url)
        await upsert_provider(
            pool,
            name=name,
            base_url=base_url,
            source="cmr",
        )
        count += 1

    logger.info("Discovered %d CMR providers", count)
    return count


async def seed_static_providers(pool: asyncpg.Pool) -> int:
    """Upsert the statically-defined providers (idempotent).

    Returns:
        The number of static providers seeded.
    """
    for provider in STATIC_PROVIDERS:
        await upsert_provider(
            pool,
            name=provider["name"],
            base_url=provider["base_url"],
            source=provider["source"],
        )
    logger.info("Seeded %d static providers", len(STATIC_PROVIDERS))
    return len(STATIC_PROVIDERS)


async def run_discovery(pool: asyncpg.Pool) -> None:
    """Run the full discovery routine used at application startup.

    Seeds static providers unconditionally; a CMR failure is logged and
    swallowed so the service still starts with the static registry available.
    """
    try:
        await discover_cmr_providers(pool)
    except Exception:  # noqa: BLE001 - startup must be resilient to CMR outages
        logger.exception("CMR provider discovery failed; continuing with static providers")

    await seed_static_providers(pool)


def _name_from_href(href: str) -> str:
    """Derive a readable provider name from a STAC child href."""
    return href.rstrip("/").rsplit("/", 1)[-1] or href
