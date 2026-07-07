"""Search route — Phase 1 stub.

Accepts the full :class:`STACSearchRequest` contract and returns an empty,
well-formed :class:`STACSearchResponse`. The real fan-out, normalization, and
ranking logic is added in Phase 2+.
"""

from fastapi import APIRouter

from app.schemas.search import STACSearchRequest, STACSearchResponse

router = APIRouter(tags=["search"])


@router.post("/search", response_model=STACSearchResponse)
async def search(request: STACSearchRequest) -> STACSearchResponse:
    """Validate a federated search request and return an empty response.

    Phase 1 only validates and echoes the shape; no catalogs are queried yet.
    """
    return STACSearchResponse(
        items=[],
        total=0,
        sources={},
        query_time_ms=0.0,
    )
