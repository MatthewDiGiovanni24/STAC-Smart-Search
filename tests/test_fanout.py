"""Offline tests for fan-out orchestration.

The DB, adapters, and embedder are all patched, so these run without a database
or network. They verify shortlist routing, per-provider status (the CMR-collapse
fix), timeout/error classification, and the no-text path.
"""

import pytest

from app.adapters.base import AdapterError, AdapterTimeout
from app.schemas.search import NormalizedSTACItem, STACSearchRequest
from app.services import fanout


def _item(cid: str, source: str) -> NormalizedSTACItem:
    return NormalizedSTACItem(id=cid, collection="c", catalog_source=source)


class _FakeAdapter:
    """Records the scoped request and returns configured items or raises."""

    registry: dict[str, "_FakeAdapter"] = {}
    behavior: dict[str, tuple] = {}  # base_url -> ("items", [...]) | ("timeout",) | ("error",)

    def __init__(self, base_url: str, source: str | None = None):
        self.base_url = base_url
        self.source = source
        self.received_collections = None
        _FakeAdapter.registry[base_url] = self

    async def search(self, request):
        self.received_collections = request.collections
        kind, *rest = _FakeAdapter.behavior.get(self.base_url, ("items", []))
        if kind == "timeout":
            raise AdapterTimeout("boom")
        if kind == "error":
            raise AdapterError("boom")
        return rest[0]


async def _identity_rank(pool, items, query_vector):
    return items


async def _identity_enrich(pool, items):
    return items


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _FakeAdapter.registry.clear()
    _FakeAdapter.behavior.clear()
    monkeypatch.setattr(fanout, "GenericSTACAdapter", _FakeAdapter)
    # Default: no candidates / no providers; individual tests override.
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([]))
    monkeypatch.setattr(fanout, "list_active_providers", _make_async([]))
    monkeypatch.setattr(fanout, "embed_query", lambda text: [0.0] * 512)
    # These tests cover orchestration, not ranking; keep rank a passthrough.
    monkeypatch.setattr(fanout, "rank_items", _identity_rank)
    # Collection context needs a real DB; orchestration doesn't depend on it.
    monkeypatch.setattr(fanout, "enrich_items_with_collection_context", _identity_enrich)


def _make_async(return_value):
    async def _fn(*args, **kwargs):
        return return_value
    return _fn


def _req(text=None, limit=20):
    return STACSearchRequest(
        bbox=[-93.5, 29.5, -90.5, 31.0], datetime="2023-06-01/2023-09-01", text=text, limit=limit
    )


PROVIDERS = [
    {"id": 1, "name": "LPCLOUD", "base_url": "https://cmr/stac/LPCLOUD", "source": "cmr"},
    {"id": 2, "name": "POCLOUD", "base_url": "https://cmr/stac/POCLOUD", "source": "cmr"},
    {"id": 3, "name": "AWS Earth Search", "base_url": "https://es/v1", "source": "earth_search"},
]


@pytest.mark.asyncio
async def test_shortlist_routes_to_owning_providers_with_scoped_collections(monkeypatch):
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        {"provider_id": 1, "id": "HLSL30_2.0"},
        {"provider_id": 1, "id": "HLSS30_2.0"},
        {"provider_id": 2, "id": "SOME_POC"},
    ]))
    _FakeAdapter.behavior["https://cmr/stac/LPCLOUD"] = ("items", [_item("a", "cmr")])
    _FakeAdapter.behavior["https://cmr/stac/POCLOUD"] = ("items", [_item("b", "cmr")])

    resp = await fanout.fanout_search(_req(text="flood"), pool=object())

    # only the two shortlisted providers were queried; earth_search was NOT
    assert set(resp.sources) == {"LPCLOUD", "POCLOUD"}
    # each provider got exactly its own shortlisted collection ids
    assert _FakeAdapter.registry["https://cmr/stac/LPCLOUD"].received_collections == ["HLSL30_2.0", "HLSS30_2.0"]
    assert _FakeAdapter.registry["https://cmr/stac/POCLOUD"].received_collections == ["SOME_POC"]
    assert resp.total == 2


@pytest.mark.asyncio
async def test_cmr_children_not_collapsed_and_status_classified(monkeypatch):
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        {"provider_id": 1, "id": "X"},
        {"provider_id": 2, "id": "Y"},
        {"provider_id": 3, "id": "Z"},
    ]))
    _FakeAdapter.behavior["https://cmr/stac/LPCLOUD"] = ("items", [_item("a", "cmr")])
    _FakeAdapter.behavior["https://cmr/stac/POCLOUD"] = ("timeout",)
    _FakeAdapter.behavior["https://es/v1"] = ("error",)

    resp = await fanout.fanout_search(_req(text="flood"), pool=object())

    # Two CMR catalogs reported SEPARATELY (the collapse bug), plus earth_search.
    assert resp.sources == {"LPCLOUD": "ok", "POCLOUD": "timeout", "AWS Earth Search": "error"}
    assert resp.total == 1  # only LPCLOUD returned an item


@pytest.mark.asyncio
async def test_no_text_query_skips_embedding(monkeypatch):
    calls = {"n": 0}

    def _spy(text):
        calls["n"] += 1
        return [0.0] * 512

    monkeypatch.setattr(fanout, "embed_query", _spy)
    captured = {}

    async def _capture_candidates(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(fanout, "get_candidate_collections", _capture_candidates)

    resp = await fanout.fanout_search(_req(text=None), pool=object())

    assert calls["n"] == 0                       # no embedding when no text
    assert captured["search_embedding"] is None  # spatial/temporal-only shortlist
    assert resp.total == 0 and resp.sources == {}


@pytest.mark.asyncio
async def test_empty_shortlist_returns_empty_without_fanout(monkeypatch):
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([]))

    resp = await fanout.fanout_search(_req(text="flood"), pool=object())

    assert resp.total == 0
    assert resp.sources == {}
    assert _FakeAdapter.registry == {}  # no adapters constructed / no catalogs hit


@pytest.mark.asyncio
async def test_ranking_applied_then_truncated_to_limit(monkeypatch):
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([{"provider_id": 1, "id": "X"}]))
    _FakeAdapter.behavior["https://cmr/stac/LPCLOUD"] = (
        "items",
        [_item("a", "cmr"), _item("b", "cmr"), _item("c", "cmr")],
    )

    called = {}

    async def _rerank(pool, items, query_vector):
        called["vector"] = query_vector
        return list(reversed(items))  # deterministic reorder to prove ranking ran

    monkeypatch.setattr(fanout, "rank_items", _rerank)

    resp = await fanout.fanout_search(_req(text="flood", limit=2), pool=object())

    assert called["vector"] is not None                 # ranking received the query vector
    assert [it.id for it in resp.items] == ["c", "b"]    # reranked order, then truncated to 2
    assert resp.total == 2


@pytest.mark.asyncio
async def test_batch_path_applies_the_same_lane_split(monkeypatch):
    """The batch path must not disagree with the stream about lane budgets."""
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        {"provider_id": 1, "id": "SENTINEL_A", "title": "SENTINEL_A", "is_exact": True},
        {"provider_id": 2, "id": "SENTINEL_B", "title": "SENTINEL_B", "is_exact": True},
        {"provider_id": 3, "id": "OTHER", "title": "OTHER", "is_exact": False},
    ]))
    _FakeAdapter.behavior["https://cmr/stac/LPCLOUD"] = ("items", [_item(f"ea-{i}", "cmr") for i in range(75)])
    _FakeAdapter.behavior["https://cmr/stac/POCLOUD"] = ("items", [_item(f"eb-{i}", "cmr") for i in range(75)])
    _FakeAdapter.behavior["https://es/v1"] = ("items", [_item(f"sem-{i}", "earth_search") for i in range(75)])

    resp = await fanout.fanout_search(_req(text="sentinel", limit=100), pool=object())

    lanes = [it.properties["match_type"] for it in resp.items]
    assert lanes.count("exact") == 50
    assert lanes.count("semantic") == 50
    assert resp.total == 100
    # Blocked ordering: the whole exact block precedes the semantic block.
    assert lanes == ["exact"] * 50 + ["semantic"] * 50


def test_blocked_order_pins_by_fine_tier_over_relevance():
    """Display order follows match_tier, not raw cosine — so the badge matches
    the order. A fuzzy item outranks a higher-scoring pure-semantic one; a
    strong-cosine semantic hit never jumps a literal or fuzzy match."""
    def mk(cid: str, tier: str, score: float) -> NormalizedSTACItem:
        it = NormalizedSTACItem(id=cid, collection="c", catalog_source="cmr")
        it.properties["match_tier"] = tier
        it.relevance_score = score
        return it

    items = [
        mk("sem", "semantic", 0.99),    # highest cosine, weakest tier
        mk("fuz", "fuzzy", 0.10),       # fuzzy must display above semantic
        mk("sub", "substring", 0.05),
        mk("exa", "exact", 0.01),       # lowest cosine, strongest tier
    ]
    ordered = fanout._blocked_order(items)
    assert [it.id for it in ordered] == ["exa", "sub", "fuz", "sem"]


@pytest.mark.asyncio
async def test_stale_candidate_provider_is_skipped(monkeypatch):
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        {"provider_id": 999, "id": "orphan"},  # provider not in the active set
    ]))

    resp = await fanout.fanout_search(_req(text="flood"), pool=object())

    assert resp.sources == {}
    assert resp.total == 0
