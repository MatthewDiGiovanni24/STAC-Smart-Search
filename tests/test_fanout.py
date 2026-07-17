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


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _FakeAdapter.registry.clear()
    _FakeAdapter.behavior.clear()
    monkeypatch.setattr(fanout, "GenericSTACAdapter", _FakeAdapter)
    # Default: no candidates / no providers; individual tests override.
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([]))
    monkeypatch.setattr(fanout, "list_active_providers", _make_async([]))
    monkeypatch.setattr(fanout, "embed_query", lambda text: [0.0] * 512)


def _make_async(return_value):
    async def _fn(*args, **kwargs):
        return return_value
    return _fn


def _req(text=None):
    return STACSearchRequest(bbox=[-93.5, 29.5, -90.5, 31.0], datetime="2023-06-01/2023-09-01", text=text)


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
async def test_stale_candidate_provider_is_skipped(monkeypatch):
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        {"provider_id": 999, "id": "orphan"},  # provider not in the active set
    ]))

    resp = await fanout.fanout_search(_req(text="flood"), pool=object())

    assert resp.sources == {}
    assert resp.total == 0
