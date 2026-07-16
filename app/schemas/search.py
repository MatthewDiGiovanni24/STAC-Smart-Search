"""Pydantic v2 schemas for the federated search request/response cycle.

These are the contract types the rest of the system maps into.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Per-source outcome reported back in a search response.
SourceStatus = Literal["ok", "timeout", "error"]


class STACSearchRequest(BaseModel):
    """A single federated STAC search request.

    Fanned out to every selected catalog adapter. ``bbox`` and
    ``datetime`` are required; free text and collection/source filters are
    optional.
    """

    bbox: list[float] = Field(
        ...,
        description="Bounding box as [west, south, east, north] in WGS84 degrees.",
    )
    datetime: str = Field(
        ...,
        description="ISO 8601 datetime or interval, e.g. '2024-01-01/2024-12-31'.",
    )
    text: str | None = Field(
        default=None,
        description="Optional free-text query used for semantic relevance ranking.",
    )
    collections: list[str] | None = Field(
        default=None,
        description="Optional list of collection IDs to restrict the search to.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of items to return (per merged response).",
    )
    sources: list[str] | None = Field(
        default=None,
        description="Optional list of catalog sources to query. Defaults to all.",
    )

    @field_validator("bbox")
    @classmethod
    def _validate_bbox(cls, value: list[float]) -> list[float]:
        if len(value) != 4:
            raise ValueError("bbox must contain exactly 4 elements: [west, south, east, north]")
        return value


class NormalizedSTACItem(BaseModel):
    """A STAC item normalized into the unified federated schema.

    Every catalog adapter maps its native response into this shape.
    ``relevance_score`` is populated by the ranking stage; ``raw_source`` retains
    the original catalog payload for debugging and re-normalization.
    """

    # id/collection are nullable so the normalizer never raises on malformed
    # features that omit them (see app/services/normalizer.py).
    id: str | None = Field(
        default=None, description="Item identifier as reported by the source catalog."
    )
    collection: str | None = Field(
        default=None, description="Collection ID the item belongs to."
    )
    catalog_source: str = Field(..., description="Source catalog, e.g. 'cmr' or 'earth_search'.")
    geometry: dict[str, Any] | None = Field(
        default=None, description="GeoJSON geometry of the item footprint."
    )
    bbox: list[float] | None = Field(
        default=None, description="Bounding box [west, south, east, north]."
    )
    datetime: str | None = Field(
        default=None, description="Item datetime (ISO 8601)."
    )
    properties: dict[str, Any] = Field(
        default_factory=dict, description="STAC item properties (full raw properties block)."
    )
    assets: dict[str, Any] = Field(
        default_factory=dict, description="STAC item assets keyed by asset name."
    )
    # Common fields lifted out of `properties` for convenient downstream access.
    cloud_cover: float | None = Field(
        default=None, description="Cloud cover percentage (from eo:cloud_cover)."
    )
    platform: str | None = Field(
        default=None, description="Acquisition platform (from platform or sat:platform)."
    )
    instruments: list[str] | None = Field(
        default=None, description="Instruments (from instruments or sat:instrument)."
    )
    constellation: str | None = Field(
        default=None, description="Constellation (from sat:constellation)."
    )
    bands: list[dict] | None = Field(
        default=None, description="Spectral bands (from eo:bands); null when absent."
    )
    relevance_score: float | None = Field(
        default=None, description="Semantic relevance score, populated by ranking."
    )
    raw_source: dict[str, Any] | None = Field(
        default=None, description="Original, unmodified catalog response for this item."
    )


class STACSearchResponse(BaseModel):
    """The merged, ranked response returned to the client."""

    items: list[NormalizedSTACItem] = Field(
        default_factory=list, description="Normalized items, ranked by relevance."
    )
    total: int = Field(default=0, description="Total number of items returned.")
    sources: dict[str, SourceStatus] = Field(
        default_factory=dict,
        description="Per-source outcome map, e.g. {'cmr': 'ok', 'earth_search': 'timeout'}.",
    )
    query_time_ms: float = Field(
        default=0.0, description="Wall-clock time to service the request, milliseconds."
    )
