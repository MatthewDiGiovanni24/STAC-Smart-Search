"""Offline tests for item-level reranking.

The DAL (collection context, item-embedding cache) and the embedder are patched,
so these run without a database or network. They verify _item_text determinism,
sort order / relevance_score, and that cache hits skip re-embedding.
"""

import hashlib

import pytest

from app.schemas.search import NormalizedSTACItem
from app.services import ranking


def _item(cid, source="cmr", collection="col", **kw):
    return NormalizedSTACItem(id=cid, collection=collection, catalog_source=source, **kw)


# --- determinism of _item_text ---------------------------------------------


def test_item_text_is_order_independent_for_lists():
    a = _item("x", instruments=["b", "a", "c"], platform="p")
    b = _item("x", instruments=["c", "a", "b"], platform="p")
    # instruments sorted internally -> identical text regardless of input order
    assert ranking._item_text(a, {}) == ranking._item_text(b, {})


def test_item_text_stable_across_calls():
    it = _item("x", platform="landsat-8", constellation="landsat", datetime="2023-08-01T00:00:00Z")
    assert ranking._item_text(it, {}) == ranking._item_text(it, {})


def test_item_text_enriched_with_collection_context():
    it = _item("x", collection="hlsl30", platform="landsat-8")
    text = ranking._item_text(it, {"hlsl30": ("HLS Landsat", "surface reflectance")})
    assert "HLS Landsat" in text and "surface reflectance" in text and "landsat-8" in text


# --- ranking behavior -------------------------------------------------------


def _fake_embed(texts):
    """Deterministic 2-d embedder keyed on content."""
    out = []
    for t in texts:
        if "flood" in t:
            out.append([1.0, 0.0])
        elif "ice" in t:
            out.append([0.0, 1.0])
        else:
            out.append([0.5, 0.5])
    return out


def _async_return(value):
    async def _fn(*a, **k):
        return value
    return _fn


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(ranking, "fetch_collection_context", _async_return({}))
    monkeypatch.setattr(ranking, "fetch_item_embeddings", _async_return({}))
    monkeypatch.setattr(ranking, "upsert_item_embeddings", _async_return(None))
    monkeypatch.setattr(ranking, "embed_texts", _fake_embed)


@pytest.mark.asyncio
async def test_ranks_by_cosine_and_sets_relevance_score(patched):
    items = [
        _item("ice", platform="arctic sea ice extent"),
        _item("flood", platform="flood inundation mapping"),
        _item("mid", platform="generic land cover"),
    ]
    ranked = await ranking.rank_items(pool=object(), items=items, query_vector=[1.0, 0.0])

    assert [it.id for it in ranked] == ["flood", "mid", "ice"]  # sorted by relevance desc
    assert ranked[0].relevance_score == pytest.approx(1.0)
    assert ranked[1].relevance_score == pytest.approx(0.5)
    assert ranked[2].relevance_score == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_no_query_vector_is_noop(patched):
    items = [_item("a", platform="x"), _item("b", platform="y")]
    out = await ranking.rank_items(pool=object(), items=items, query_vector=None)
    assert [it.id for it in out] == ["a", "b"]
    assert all(it.relevance_score is None for it in out)


@pytest.mark.asyncio
async def test_cache_hit_skips_embedding(monkeypatch):
    monkeypatch.setattr(ranking, "fetch_collection_context", _async_return({}))
    monkeypatch.setattr(ranking, "upsert_item_embeddings", _async_return(None))

    flood = _item("flood", platform="flood inundation mapping")
    ice = _item("ice", platform="arctic sea ice extent")

    # Seed the cache for the flood item with a matching text hash + stored vector.
    flood_hash = hashlib.sha256(ranking._item_text(flood, {}).encode()).hexdigest()
    key = (flood.catalog_source, flood.collection, flood.id)
    monkeypatch.setattr(ranking, "fetch_item_embeddings", _async_return({key: (flood_hash, "[1.0,0.0]")}))

    embedded = []

    def _spy_embed(texts):
        embedded.extend(texts)
        return _fake_embed(texts)

    monkeypatch.setattr(ranking, "embed_texts", _spy_embed)

    ranked = await ranking.rank_items(pool=object(), items=[flood, ice], query_vector=[1.0, 0.0])

    # flood served from cache (its text NOT embedded); ice was a miss (embedded)
    assert not any("flood" in t for t in embedded)
    assert any("ice" in t for t in embedded)
    assert ranked[0].id == "flood" and ranked[0].relevance_score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_empty_metadata_text_scores_none_and_sorts_last(patched):
    scored = _item("flood", platform="flood inundation mapping")
    empty = _item("empty")  # no platform/instruments/etc -> empty text
    ranked = await ranking.rank_items(pool=object(), items=[empty, scored], query_vector=[1.0, 0.0])
    assert ranked[0].id == "flood"
    assert ranked[-1].id == "empty" and ranked[-1].relevance_score is None
