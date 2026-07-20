"""SSE serialization for the streaming search endpoint.

Turns the structured events from :func:`app.services.fanout.stream_search` into
``text/event-stream`` frames. Keeps the wire projection (which item fields go on
the ``item`` event) out of the route handler.
"""

import json
from collections.abc import AsyncIterator

import asyncpg

from app.schemas.search import NormalizedSTACItem, STACSearchRequest
from app.services.fanout import stream_search


def _item_payload(item: NormalizedSTACItem) -> dict:
    """Project a normalized item onto the fields sent on an SSE ``item`` event."""
    return {
        "id": item.id,
        "collection": item.collection,
        "collection_title": item.collection_title,
        "catalog_source": item.catalog_source,
        "datetime": item.datetime,
        "bbox": item.bbox,
        "relevance_score": item.relevance_score,
        "cloud_cover": item.cloud_cover,
        "platform": item.platform,
        "assets": item.assets,
    }


def _sse_frame(event: str, data: dict) -> str:
    """Format one SSE frame (event name + JSON data, terminated by a blank line)."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def sse_search_stream(
    request: STACSearchRequest, pool: asyncpg.Pool
) -> AsyncIterator[str]:
    """Yield SSE frames: one ``item`` per result, then a final ``meta`` frame."""
    async for event, payload in stream_search(request, pool):
        if event == "item":
            yield _sse_frame("item", _item_payload(payload))
        else:
            yield _sse_frame("meta", payload)
