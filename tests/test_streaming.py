"""Offline tests for SSE streaming search (stream_search + sse_search_stream).

DB, adapters, embedder, scorer, and registry status are patched, so these run
without a database or network. They verify the event sequence (items then one
meta), the global ranking in meta, per-provider health, and valid SSE framing.
"""

import json
from types import SimpleNamespace

import pytest

from app.adapters.base import AdapterError, AdapterTimeout
from app.schemas.search import NormalizedSTACItem, STACSearchRequest
from app.services import fanout, streaming


def _item(cid: str, source: str = "cmr") -> NormalizedSTACItem:
    return NormalizedSTACItem(id=cid, collection="col", catalog_source=source)


class _FakeAdapter:
    behavior: dict[str, tuple] = {}

    def __init__(self, base_url: str, source: str | None = None):
        self.base_url = base_url
        self.source = source

    async def search(self, request):
        kind, *rest = _FakeAdapter.behavior.get(self.base_url, ("items", []))
        if kind == "timeout":
            raise AdapterTimeout("t")
        if kind == "error":
            raise AdapterError("e")
        return rest[0]


def _make_async(value):
    async def _fn(*a, **k):
        return value
    return _fn


PROVIDERS = [
    {"id": 1, "name": "LPCLOUD", "base_url": "https://cmr/stac/LPCLOUD", "source": "cmr"},
    {"id": 2, "name": "POCLOUD", "base_url": "https://cmr/stac/POCLOUD", "source": "cmr"},
]


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _FakeAdapter.behavior.clear()
    monkeypatch.setattr(fanout, "GenericSTACAdapter", _FakeAdapter)
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "embed_query", lambda text: [1.0, 0.0])
    monkeypatch.setattr(fanout, "get_registry_status", _make_async(SimpleNamespace(ready=True)))

    async def _score(pool, items, qv):
        # deterministic: score = trailing integer of the id ("it-3" -> 3.0)
        for it in items:
            it.relevance_score = float(int(it.id.rsplit("-", 1)[-1]))
        return items

    monkeypatch.setattr(fanout, "score_items", _score)


def _req(text="flood"):
    return STACSearchRequest(bbox=[-93.5, 29.5, -90.5, 31.0], datetime="2023-06-01/2023-09-01", text=text)


@pytest.mark.asyncio
async def test_stream_yields_items_then_single_meta(monkeypatch):
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async(
        [{"provider_id": 1, "id": "A"}, {"provider_id": 2, "id": "B"}]))
    _FakeAdapter.behavior["https://cmr/stac/LPCLOUD"] = ("items", [_item("it-1"), _item("it-3")])
    _FakeAdapter.behavior["https://cmr/stac/POCLOUD"] = ("items", [_item("it-2")])

    events = [ev async for ev in fanout.stream_search(_req(), pool=object())]

    kinds = [e[0] for e in events]
    assert kinds.count("meta") == 1 and kinds[-1] == "meta"        # exactly one meta, at the end
    assert {e[1].id for e in events if e[0] == "item"} == {"it-1", "it-2", "it-3"}

    meta = events[-1][1]
    assert meta["ranked_ids"] == ["it-3", "it-2", "it-1"]          # global ranking by score desc
    assert meta["total"] == 3
    assert meta["registry_warm"] is True
    assert set(meta["sources"]) == {"LPCLOUD", "POCLOUD"}


@pytest.mark.asyncio
async def test_meta_emitted_even_with_zero_items(monkeypatch):
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([]))
    events = [ev async for ev in fanout.stream_search(_req(), pool=object())]
    assert len(events) == 1 and events[0][0] == "meta"
    assert events[0][1]["total"] == 0 and events[0][1]["ranked_ids"] == []


@pytest.mark.asyncio
async def test_provider_timeout_and_error_reported_not_raised(monkeypatch):
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async(
        [{"provider_id": 1, "id": "A"}, {"provider_id": 2, "id": "B"}]))
    _FakeAdapter.behavior["https://cmr/stac/LPCLOUD"] = ("timeout",)
    _FakeAdapter.behavior["https://cmr/stac/POCLOUD"] = ("error",)

    events = [ev async for ev in fanout.stream_search(_req(), pool=object())]
    meta = events[-1][1]
    assert meta["sources"] == {"LPCLOUD": "timeout", "POCLOUD": "error"}
    assert meta["total"] == 0


@pytest.mark.asyncio
async def test_sse_frames_are_wellformed(monkeypatch):
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([{"provider_id": 1, "id": "A"}]))
    _FakeAdapter.behavior["https://cmr/stac/LPCLOUD"] = ("items", [_item("it-1")])

    frames = [f async for f in streaming.sse_search_stream(_req(), pool=object())]
    text = "".join(frames)
    assert "event: item\n" in text and "event: meta\n" in text
    for frame in frames:
        assert frame.endswith("\n\n")
        data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
        json.loads(data_line[len("data: "):])  # valid JSON payload
