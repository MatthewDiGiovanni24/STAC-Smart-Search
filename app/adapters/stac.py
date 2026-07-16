"""
Generic STAC Adapter that can be used for any STAC-compliant catalog
Returns normalized STAC items in the unified schema
"""

import httpx
import logging
from app.adapters.base import STACAdapter
from app.schemas.search import NormalizedSTACItem, STACSearchRequest
from app.services.normalizer import normalize_item

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
        if request.collections:
            body["collections"] = request.collections

        url = self.base_url.rstrip("/") + "/search"

        logger.debug(f"Sending to {url}: {body}")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json=body)
            response.raise_for_status()

            data = response.json()
            features = data.get("features", [])

            # All field mapping lives in the normalizer; the adapter only does
            # HTTP and pulls the features[] array out of the response.
            return [normalize_item(feature, self.source) for feature in features]

        except httpx.TimeoutException as e:
            raise RuntimeError(f"Timeout error while searching {self.source}: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"HTTP error while searching {self.source}: {e}") from e
