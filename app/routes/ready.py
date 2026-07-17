"""Readiness route — reports collection-registry warmth.

Distinct from ``/health`` (liveness): ``/ready`` tells callers whether the
semantic pre-filter has data yet. On first boot the registry warms in the
background, during which searches return empty results by design.
"""

import asyncpg
from fastapi import APIRouter, Depends

from app.database import get_pool
from app.schemas.collection import RegistryStatus
from app.services.registry_state import get_registry_status

router = APIRouter(tags=["system"])


@router.get("/ready", response_model=RegistryStatus)
async def ready(pool: asyncpg.Pool = Depends(get_pool)) -> RegistryStatus:
    """Return whether the collection registry is warm and how much is indexed."""
    return await get_registry_status(pool)
