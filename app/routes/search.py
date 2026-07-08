"""Search route
Accepts a federated search request and fans it out to all selected catalog adapters.
"""

import asyncpg

from fastapi import APIRouter, Depends

from app.schemas.search import STACSearchRequest, STACSearchResponse

from app.services.fanout import fanout_search

from app.database import get_pool

router = APIRouter(tags=["search"])


@router.post("/search", response_model=STACSearchResponse)
async def search(body: STACSearchRequest, pool: asyncpg.Pool = Depends(get_pool)) -> STACSearchResponse:
    return await fanout_search(body, pool)
