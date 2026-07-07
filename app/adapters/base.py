"""Abstract base class for STAC catalog adapters.

Every catalog integration (NASA CMR, Microsoft Planetary Computer, AWS Earth
Search, ...) implements this interface in Phase 2. Defining it now fixes the
contract the search fan-out and normalization layers will depend on.
"""

from abc import ABC, abstractmethod

from app.schemas.search import NormalizedSTACItem, STACSearchRequest


class STACAdapter(ABC):
    """Interface all catalog adapters must implement.

    A concrete adapter knows how to translate a :class:`STACSearchRequest` into
    the query dialect of one catalog, execute it, and map the response into the
    unified :class:`NormalizedSTACItem` schema.
    """

    #: Stable source identifier for this catalog, e.g. ``"cmr"``. Used to tag
    #: normalized items and to match against ``STACSearchRequest.sources``.
    source: str

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    @abstractmethod
    async def search(self, request: STACSearchRequest) -> list[NormalizedSTACItem]:
        """Execute a search against this catalog and return normalized items.

        Implementations must be fully asynchronous (no blocking HTTP/DB calls)
        so the orchestrator can fan out to many catalogs concurrently.

        Args:
            request: The federated search request to translate and execute.

        Returns:
            A list of items normalized into the unified schema. Implementations
            should raise on transport/parse failures so the orchestrator can
            record a per-source ``"error"`` / ``"timeout"`` status.
        """
        raise NotImplementedError
