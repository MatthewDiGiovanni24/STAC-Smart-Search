"""FastAPI application entry point.

Wires together configuration, the database pool, provider discovery, and the
HTTP routes. Startup/shutdown are managed via the lifespan context manager.
"""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.config import get_settings
from app.database import close_db_pool, init_db_pool
from app.routes import catalogs, health, search
from app.services.discovery import run_discovery


def _configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize the DB pool and run provider discovery on startup."""
    _configure_logging()
    logger = logging.getLogger(__name__)

    logger.info("Starting stac-federated")
    pool = await init_db_pool()
    await run_discovery(pool)
    logger.info("Startup complete")

    try:
        yield
    finally:
        await close_db_pool()
        logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="stac-federated",
        description="Federated STAC catalog discovery service.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(catalogs.router)
    app.include_router(search.router)

    return app


app = create_app()
