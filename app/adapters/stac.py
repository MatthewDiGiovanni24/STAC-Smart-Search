"""
Generic STAC Adapter that can be used for any STAC-compliant catalog
Returns normalized STAC items in the unified schema
"""

import httpx
import logging
from app.adapters.base import STACAdapter
from app.schemas.search import NormalizedSTACItem, STACSearchRequest

logger = logging.getLogger(__name__)


class GenericSTACAdapter(STACAdapter):

    def __init__(self, base_url: str, source: str=None) -> None:
        super().__init__(base_url)
        self.source = source or self.base_url

    async def search(self, request: STACSearchRequest) -> list[NormalizedSTACItem]:
        body = {
            "bbox": request.bbox,
            "datetime": request.datetime,
            "limit": request.limit,
        }
        if request.text:
            body["q"] = request.text
        if request.collections:
            body["collections"] = request.collections

        url = self.base_url.rstrip("/") + "/search"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json=body)
            response.raise_for_status()

            data = response.json()
            features = data.get("features", [])

            normalized_items = []
            for feature in features:
                normalized_item = NormalizedSTACItem(
                    id=feature.get("id"),
                    collection=feature.get("collection"),
                    catalog_source=self.source,
                    geometry=feature.get("geometry"),
                    bbox=feature.get("bbox"),
                    datetime=feature.get("properties", {}).get("datetime"),
                    properties=feature.get("properties"),
                    assets=feature.get("assets"),
                    relevance_score=None,  # To be populated later
                    raw_source=feature,  # Retain the original feature for debugging
                )
                normalized_items.append(normalized_item)
        
            return normalized_items
        
        except httpx.TimeoutException as e:
            raise RuntimeError(f"Timeout error while searching {self.source}: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"HTTP error while searching {self.source}: {e}") from e
