"""Management CLI for stac-federated.

Replaces the old ``seed_collections.py``. Use it to refresh the collection
registry out-of-band (e.g. from cron) instead of relying on the startup crawl.

    python -m app.cli refresh-registry [--discover] [--max-cmr N] [--per-provider N]
"""

import argparse
import asyncio
import logging

from app.config import get_settings
from app.database import close_db_pool, init_db_pool
from app.services.collection_crawler import refresh_collection_registry
from app.services.discovery import run_discovery


async def _refresh_registry(args: argparse.Namespace) -> None:
    pool = await init_db_pool()
    try:
        if args.discover:
            await run_discovery(pool)
        summary = await refresh_collection_registry(
            pool,
            max_cmr_collections=args.max_cmr,
            per_provider_limit=args.per_provider,
        )
        print(summary)
    finally:
        await close_db_pool()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli", description="stac-federated management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh-registry", help="Crawl catalogs and (re)build the collection registry")
    refresh.add_argument("--discover", action="store_true", help="Run provider discovery first")
    refresh.add_argument("--max-cmr", type=int, default=None, help="Cap CMR collections (dev)")
    refresh.add_argument("--per-provider", type=int, default=None, help="Cap collections per non-CMR provider")

    args = parser.parse_args()
    logging.basicConfig(level=get_settings().log_level.upper())

    if args.command == "refresh-registry":
        asyncio.run(_refresh_registry(args))


if __name__ == "__main__":
    main()
