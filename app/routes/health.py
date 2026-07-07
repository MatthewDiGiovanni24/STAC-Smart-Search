"""Health check route."""

from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns a static OK payload."""
    return {"status": "ok"}
