from pydantic import BaseModel
from typing import List, Optional


class RegistryStatus(BaseModel):
    """Warmth of the collection registry, surfaced to callers.

    Lets clients distinguish "still indexing on first boot" (empty results are
    expected) from "genuinely no matches" or "broken".
    """

    ready: bool                      # true once at least one collection is embedded
    phase: str                       # idle | warming | ready | error
    collections_indexed: int         # rows in the collections table
    collections_embedded: int        # rows with an embedding
    cmr_indexed: int                 # rows owned by a CMR provider (live count)
    error: Optional[str] = None      # last crawl error, if any
    last_refresh: Optional[str] = None  # ISO timestamp of last completed refresh


class CollectionMetadata(BaseModel):
    id: str
    provider_id: int
    title: Optional[str] = None
    description: Optional[str] = None
    
    # Expecting [min_x, min_y, max_x, max_y]
    spatial_extent: Optional[List[float]] = None 
    
    # Expecting [start_time, end_time] 
    temporal_extent: Optional[List[Optional[str]]] = None 
    
    # The 384-dimension vector (if we decide to pass it through)
    embedding: Optional[List[float]] = None