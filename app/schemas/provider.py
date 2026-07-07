"""Pydantic schema for provider records returned by the /catalogs endpoint."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProviderOut(BaseModel):
    """A registered catalog provider as exposed via the API."""

    # Allow construction directly from asyncpg.Record / mapping rows.
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Internal provider registry ID.")
    name: str = Field(..., description="Human-readable provider name.")
    base_url: str = Field(..., description="STAC root URL for this provider.")
    source: str = Field(
        ..., description="Origin of the provider, e.g. 'cmr', 'planetary_computer', 'earth_search'."
    )
    spatial_extent: dict[str, Any] | None = Field(
        default=None, description="Provider spatial extent (GeoJSON-ish), if known."
    )
    temporal_extent: dict[str, Any] | None = Field(
        default=None, description="Provider temporal extent, if known."
    )
    last_discovered_at: datetime = Field(
        ..., description="Timestamp of the most recent discovery/refresh."
    )
    is_active: bool = Field(..., description="Whether the provider is currently active.")
