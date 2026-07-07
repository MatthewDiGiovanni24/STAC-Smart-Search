"""Async Postgres connection pool management.

The application uses a single shared ``asyncpg`` pool for all runtime queries.
The pool is created during FastAPI's lifespan startup and closed on shutdown.
Schema management (DDL) is handled separately by Alembic migrations.
"""

import logging

import asyncpg

from app.config import get_settings

logger = logging.getLogger(__name__)

# Module-level pool. Populated by ``init_db_pool`` during app startup.
_pool: asyncpg.Pool | None = None


async def init_db_pool() -> asyncpg.Pool:
    """Create the shared asyncpg connection pool (idempotent)."""
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    logger.info("Initializing Postgres connection pool")
    _pool = await asyncpg.create_pool(
        dsn=settings.asyncpg_url,
        min_size=1,
        max_size=10,
        command_timeout=60,
    )
    return _pool


async def close_db_pool() -> None:
    """Close the shared connection pool on shutdown."""
    global _pool
    if _pool is not None:
        logger.info("Closing Postgres connection pool")
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the initialized pool, raising if startup has not run yet.

    Suitable for use as a FastAPI dependency in route handlers.
    """
    if _pool is None:
        raise RuntimeError(
            "Database pool is not initialized. Did the application lifespan run?"
        )
    return _pool
