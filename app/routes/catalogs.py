"""Catalogs route — lists registered catalog providers."""

import asyncpg
from fastapi import APIRouter, Depends

from app.database import get_pool
from app.models.provider import list_active_providers, record_to_dict
from app.schemas.provider import ProviderOut

router = APIRouter(tags=["catalogs"])


@router.get("/catalogs", response_model=list[ProviderOut])
async def list_catalogs(pool: asyncpg.Pool = Depends(get_pool)) -> list[ProviderOut]:
    """Return all active, registered catalog providers."""
    records = await list_active_providers(pool)
    return [ProviderOut(**record_to_dict(r)) for r in records]
