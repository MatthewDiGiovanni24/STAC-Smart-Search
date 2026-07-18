"""
Generic STAC Adapter that can be used for any STAC-compliant catalog
Returns normalized STAC items in the unified schema
"""

import httpx
import logging
from app.adapters.base import AdapterError, AdapterTimeout, STACAdapter
from app.config import get_settings
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
        timeout = get_settings().adapter_timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=body)
            response.raise_for_status()

            data = response.json()
            features = data.get("features", [])

            # All field mapping lives in the normalizer; the adapter only does
            # HTTP and pulls the features[] array out of the response.
            return [normalize_item(feature, self.source) for feature in features]

        except httpx.TimeoutException as e:
            raise AdapterTimeout(f"Timeout searching {self.source} ({self.base_url}): {e}") from e
        except httpx.HTTPStatusError as e:
            raise AdapterError(
                f"HTTP {e.response.status_code} searching {self.source} ({self.base_url})"
            ) from e
        except httpx.HTTPError as e:  # connect/read/transport errors
            raise AdapterError(f"Transport error searching {self.source} ({self.base_url}): {e}") from e
