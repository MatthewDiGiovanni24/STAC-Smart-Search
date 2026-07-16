from pydantic import BaseModel
from typing import List, Optional

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