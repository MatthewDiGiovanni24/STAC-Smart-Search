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
    {"id": 3, "name": "AWS Earth Search", "base_url": "https://es/v1", "source": "earth_search"},
]

LPCLOUD, POCLOUD, EARTH_SEARCH = (p["base_url"] for p in PROVIDERS)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _FakeAdapter.behavior.clear()
    monkeypatch.setattr(fanout, "GenericSTACAdapter", _FakeAdapter)
    monkeypatch.setattr(fanout, "list_active_providers", _make_async(PROVIDERS))
    monkeypatch.setattr(fanout, "embed_query", lambda text: [1.0, 0.0])
    monkeypatch.setattr(fanout, "get_registry_status", _make_async(SimpleNamespace(ready=True)))

    async def _enrich(pool, items):
        # Collection context needs a real DB; these tests cover orchestration.
        return items

    monkeypatch.setattr(fanout, "enrich_items_with_collection_context", _enrich)

    async def _score(pool, items, qv):
        # deterministic: score = trailing integer of the id ("it-3" -> 3.0)
        for it in items:
            it.relevance_score = float(int(it.id.rsplit("-", 1)[-1]))
        return items

    monkeypatch.setattr(fanout, "score_items", _score)


def _req(text="flood", limit=100):
    return STACSearchRequest(
        bbox=[-93.5, 29.5, -90.5, 31.0], datetime="2023-06-01/2023-09-01", text=text, limit=limit
    )


def _collection(provider_id: int, cid: str, is_exact: bool) -> dict:
    return {"provider_id": provider_id, "id": cid, "title": cid, "is_exact": is_exact}


def _items(prefix: str, count: int) -> tuple:
    return ("items", [_item(f"{prefix}-{i}") for i in range(count)])


def _lanes(events) -> dict[str, list]:
    """Group streamed items by the lane they were credited to."""
    grouped: dict[str, list] = {"exact": [], "semantic": []}
    for kind, payload in events:
        if kind == "item":
            grouped[payload.properties["match_type"]].append(payload)
    return grouped


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
async def test_two_exact_providers_do_not_starve_the_semantic_lane(monkeypatch):
    """The regression: two exact providers returning 75 items each used to fill
    the shared 100-item budget and cancel the semantic lane outright.

    Needs *two* exact providers — with one, 75 items never reached the old
    TARGET_ITEMS and semantic work still got its turn.
    """
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        _collection(1, "SENTINEL_A", is_exact=True),
        _collection(2, "SENTINEL_B", is_exact=True),
        _collection(3, "OTHER", is_exact=False),
    ]))
    _FakeAdapter.behavior[LPCLOUD] = _items("ea", 75)
    _FakeAdapter.behavior[POCLOUD] = _items("eb", 75)
    _FakeAdapter.behavior[EARTH_SEARCH] = _items("sem", 75)

    events = [ev async for ev in fanout.stream_search(_req(text="SENTINEL", limit=100), pool=object())]
    lanes = _lanes(events)

    assert len(lanes["exact"]) == 50
    assert len(lanes["semantic"]) == 50      # was 0 before the fix
    assert events[-1][1]["total"] == 100
    # The semantic provider was actually queried, not cancelled mid-flight.
    assert events[-1][1]["sources"]["AWS Earth Search"] == "ok"


@pytest.mark.asyncio
async def test_exact_backfills_only_the_budget_semantic_left_unused(monkeypatch):
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        _collection(1, "SENTINEL_A", is_exact=True),
        _collection(2, "SENTINEL_B", is_exact=True),
        _collection(3, "OTHER", is_exact=False),
    ]))
    _FakeAdapter.behavior[LPCLOUD] = _items("ea", 75)
    _FakeAdapter.behavior[POCLOUD] = _items("eb", 75)
    _FakeAdapter.behavior[EARTH_SEARCH] = _items("sem", 10)   # semantic underfills

    events = [ev async for ev in fanout.stream_search(_req(text="SENTINEL", limit=100), pool=object())]
    lanes = _lanes(events)

    assert len(lanes["semantic"]) == 10      # everything semantic had
    assert len(lanes["exact"]) == 90         # 50 quota + 40 spilled
    assert events[-1][1]["total"] == 100


@pytest.mark.asyncio
async def test_semantic_backfills_when_exact_is_short(monkeypatch):
    """Backfill is symmetric — the short lane can be either one."""
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        _collection(1, "SENTINEL_A", is_exact=True),
        _collection(3, "OTHER", is_exact=False),
    ]))
    _FakeAdapter.behavior[LPCLOUD] = _items("ea", 12)
    _FakeAdapter.behavior[EARTH_SEARCH] = _items("sem", 200)

    events = [ev async for ev in fanout.stream_search(_req(text="SENTINEL", limit=100), pool=object())]
    lanes = _lanes(events)

    assert len(lanes["exact"]) == 12
    assert len(lanes["semantic"]) == 88
    assert events[-1][1]["total"] == 100


@pytest.mark.asyncio
async def test_ranking_is_blocked_exact_first_and_scores_are_untouched(monkeypatch):
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        _collection(1, "SENTINEL_A", is_exact=True),
        _collection(3, "OTHER", is_exact=False),
    ]))
    # Semantic scores beat every exact score; blocked ordering must ignore that.
    _FakeAdapter.behavior[LPCLOUD] = ("items", [_item("ex-1"), _item("ex-3")])
    _FakeAdapter.behavior[EARTH_SEARCH] = ("items", [_item("sm-9")])

    events = [ev async for ev in fanout.stream_search(_req(), pool=object())]
    ranked = events[-1][1]["ranked_ids"]

    # Exact block first (internally by score desc), then semantic — never merged.
    assert ranked == ["ex-3", "ex-1", "sm-9"]

    by_id = {e[1].id: e[1] for e in events if e[0] == "item"}
    # No exact-match score bonus: the real cosine scores survive.
    assert by_id["ex-1"].relevance_score == 1.0 and by_id["ex-3"].relevance_score == 3.0
    assert "score" not in by_id["ex-1"].properties


@pytest.mark.asyncio
async def test_no_text_query_has_no_lanes(monkeypatch):
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        _collection(1, "A", is_exact=False),
    ]))
    _FakeAdapter.behavior[LPCLOUD] = ("items", [_item("it-1"), _item("it-2")])

    events = [ev async for ev in fanout.stream_search(_req(text=None), pool=object())]
    items = [e[1] for e in events if e[0] == "item"]

    assert len(items) == 2
    assert all("match_type" not in it.properties for it in items)


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


async def _sse_events(request) -> list[tuple[str, dict]]:
    """Parse the actual SSE wire frames into (event, data) — exercises _item_payload.

    Prefer this over iterating stream_search directly: it goes through the
    serialization layer, so a field present on the model but dropped from the
    wire projection is caught. That gap is what let match_type never reach the UI.
    """
    out: list[tuple[str, dict]] = []
    async for frame in streaming.sse_search_stream(request, pool=object()):
        lines = frame.split("\n")
        event = next(ln[len("event: "):] for ln in lines if ln.startswith("event: "))
        data = next(ln[len("data: "):] for ln in lines if ln.startswith("data: "))
        out.append((event, json.loads(data)))
    return out


@pytest.mark.asyncio
async def test_sse_item_frames_carry_match_type_on_the_wire(monkeypatch):
    """Serialization-layer regression: match_type must survive _item_payload.

    The other stream tests assert on stream_search's model objects, which always
    carried match_type; nothing exercised the wire projection, so a field present
    on the model but dropped by the serializer went unnoticed.
    """
    monkeypatch.setattr(fanout, "get_candidate_collections", _make_async([
        _collection(1, "SENTINEL_A", is_exact=True),
        _collection(3, "OTHER", is_exact=False),
    ]))
    _FakeAdapter.behavior[LPCLOUD] = _items("ex", 3)
    _FakeAdapter.behavior[EARTH_SEARCH] = _items("sem", 3)

    events = await _sse_events(_req(text="SENTINEL", limit=100))
    items = [d for kind, d in events if kind == "item"]

    assert items, "expected item frames on the wire"
    assert all("match_type" in (d.get("properties") or {}) for d in items)
    for d in items:
        expected = "exact" if d["id"].startswith("ex-") else "semantic"
        assert d["properties"]["match_type"] == expected
