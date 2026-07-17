"""Search route.

Streams results as Server-Sent Events by default (progressive item events +
a final meta event with the global ranking). Falls back to the batch JSON
response when the client asks for ``Accept: application/json`` (keeps /docs and
simple curl usable).
"""

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.database import get_pool
from app.schemas.search import STACSearchRequest, STACSearchResponse
from app.services.fanout import fanout_search
from app.services.registry_state import get_registry_status
from app.services.streaming import sse_search_stream

router = APIRouter(tags=["search"])

# SSE-friendly headers: disable caching and proxy buffering so frames flush live.
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/search")
async def search(body: STACSearchRequest, request: Request, pool: asyncpg.Pool = Depends(get_pool)):
    """SSE stream by default; batch JSON when ``Accept: application/json``."""
    if "application/json" in request.headers.get("accept", ""):
        response: STACSearchResponse = await fanout_search(body, pool)
        response.registry = await get_registry_status(pool)
        return response

    return StreamingResponse(
        sse_search_stream(body, pool),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
