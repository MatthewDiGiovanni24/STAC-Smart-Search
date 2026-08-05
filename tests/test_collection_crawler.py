"""Offline unit tests for the CMR crawl logic.

These exercise the pure mapping functions (id reconstruction, provider mapping,
extent/temporal extraction) against synthetic umm_json fixtures shaped like
CMR's real ``collections.umm_json`` responses. No network is used.
"""

import pytest

from app.services.collection_crawler import (
    _cmr_bbox,
    _cmr_item_to_raw,
    _cmr_provider_map,
    _cmr_stac_id,
    _cmr_temporal,
    _search_after_bytes,
    crawl_cmr_native,
)


# --- CMR-STAC collection id reconstruction (the "Not provided" fix) ---------


def test_cmr_stac_id_normal():
    assert _cmr_stac_id("HLSL30", "2.0") == "HLSL30_2.0"
    assert _cmr_stac_id("AEHYPICNE1M", "001") == "AEHYPICNE1M_001"


def test_cmr_stac_id_not_provided_sentinel_uses_bare_shortname():
    # This was the routing bug: "Not provided" must NOT be appended.
    assert _cmr_stac_id("NESP_2015_SRW", "Not provided") == "NESP_2015_SRW"
    assert _cmr_stac_id("NESP_2015_SRW", " Not provided ") == "NESP_2015_SRW"


def test_cmr_stac_id_missing_version_uses_bare_shortname():
    assert _cmr_stac_id("SOMECOLL", None) == "SOMECOLL"


def test_cmr_stac_id_other_sentinels_kept():
    # Only "Not provided" is special; e.g. "Not Applicable" keeps the suffix.
    assert _cmr_stac_id("39564", "Not Applicable") == "39564_Not Applicable"


def test_cmr_stac_id_no_shortname_is_none():
    assert _cmr_stac_id(None, "1.0") is None
    assert _cmr_stac_id("", "1.0") is None


# --- provider mapping (short-name -> registered provider row id) -------------


def _providers():
    return [
        {"id": 10, "base_url": "https://cmr.earthdata.nasa.gov/stac/LPCLOUD", "source": "cmr"},
        {"id": 11, "base_url": "https://cmr.earthdata.nasa.gov/stac/SCIOPS/", "source": "cmr"},
        {"id": 99, "base_url": "https://cmr.earthdata.nasa.gov/stac/ALL", "source": "cmr"},
        {"id": 20, "base_url": "https://earth-search.aws.element84.com/v1", "source": "earth_search"},
    ]


def test_provider_map_builds_shortname_to_id_and_skips_all_and_non_cmr():
    m = _cmr_provider_map(_providers())
    assert m == {"LPCLOUD": 10, "SCIOPS": 11}  # ALL skipped; earth_search excluded


# --- full item mapping (provider routing + id + extents) --------------------


def _umm_item(provider_id, short_name, version, **umm):
    base = {"ShortName": short_name, "Version": version}
    base.update(umm)
    return {"meta": {"provider-id": provider_id, "concept-id": "C123-X"}, "umm": base}


def test_item_maps_to_owning_provider_and_reconstructs_id():
    m = _cmr_provider_map(_providers())
    raw = _cmr_item_to_raw(_umm_item("LPCLOUD", "HLSL30", "2.0", EntryTitle="HLS"), m)
    assert raw is not None
    assert raw["provider_id"] == 10          # routed to LPCLOUD's row, not collapsed
    assert raw["id"] == "HLSL30_2.0"


def test_item_not_provided_routes_correctly():
    m = _cmr_provider_map(_providers())
    raw = _cmr_item_to_raw(_umm_item("SCIOPS", "NESP_2015_SRW", "Not provided"), m)
    assert raw is not None
    assert raw["provider_id"] == 11
    assert raw["id"] == "NESP_2015_SRW"       # the fix — no sentinel suffix


def test_item_unmapped_provider_is_skipped():
    m = _cmr_provider_map(_providers())
    # A CMR provider with no registered STAC child cannot be routed -> None.
    assert _cmr_item_to_raw(_umm_item("GHOST_PROV", "X", "1"), m) is None


def test_item_missing_shortname_is_skipped():
    m = _cmr_provider_map(_providers())
    assert _cmr_item_to_raw(_umm_item("LPCLOUD", None, "1"), m) is None


# --- bounding box extraction ------------------------------------------------


def _with_rects(*rects):
    return {
        "SpatialExtent": {
            "HorizontalSpatialDomain": {"Geometry": {"BoundingRectangles": list(rects)}}
        }
    }


def test_cmr_bbox_single_rectangle():
    umm = _with_rects(
        {
            "WestBoundingCoordinate": -93.5,
            "SouthBoundingCoordinate": 29.5,
            "EastBoundingCoordinate": -90.5,
            "NorthBoundingCoordinate": 31.0,
        }
    )
    assert _cmr_bbox(umm) == [-93.5, 29.5, -90.5, 31.0]


def test_cmr_bbox_multiple_rectangles_takes_overall_extent():
    umm = _with_rects(
        {"WestBoundingCoordinate": -10, "SouthBoundingCoordinate": -5,
         "EastBoundingCoordinate": 0, "NorthBoundingCoordinate": 5},
        {"WestBoundingCoordinate": 2, "SouthBoundingCoordinate": -8,
         "EastBoundingCoordinate": 12, "NorthBoundingCoordinate": 3},
    )
    assert _cmr_bbox(umm) == [-10.0, -8.0, 12.0, 5.0]


def test_cmr_bbox_missing_is_none():
    assert _cmr_bbox({}) is None
    assert _cmr_bbox({"SpatialExtent": {"HorizontalSpatialDomain": {}}}) is None


# --- temporal extraction ----------------------------------------------------


def test_cmr_temporal_range():
    umm = {"TemporalExtents": [{"RangeDateTimes": [
        {"BeginningDateTime": "2001-01-01T00:00:00Z", "EndingDateTime": "2020-12-31T23:59:59Z"}
    ]}]}
    assert _cmr_temporal(umm) == ["2001-01-01T00:00:00Z", "2020-12-31T23:59:59Z"]


def test_cmr_temporal_ongoing_has_no_end():
    umm = {"TemporalExtents": [{"RangeDateTimes": [{"BeginningDateTime": "2015-01-01T00:00:00Z"}]}]}
    assert _cmr_temporal(umm) == ["2015-01-01T00:00:00Z", None]


def test_cmr_temporal_missing_is_none():
    assert _cmr_temporal({}) is None


# --- Defect 1: non-ASCII CMR-Search-After cursor must round-trip safely -------


class _FakeHeaders:
    def __init__(self, raw):
        self.raw = raw


class _FakeResp:
    """Minimal httpx.Response stand-in exposing .headers.raw and .json()."""

    def __init__(self, items, cursor):  # cursor: bytes | None
        raw = [(b"content-type", b"application/json")]
        if cursor is not None:
            raw.append((b"CMR-Search-After", cursor))
        self.headers = _FakeHeaders(raw)
        self._items = items

    def raise_for_status(self):
        return None

    def json(self):
        return {"items": self._items}


class _FakeClient:
    """Mimics the one httpx behavior that matters: str header values are
    ASCII-encoded (raising on non-ASCII), bytes values pass through."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.sent_cursors = []

    async def get(self, url, params=None, headers=None, timeout=None):
        headers = headers or {}
        for value in headers.values():
            if isinstance(value, str):
                value.encode("ascii")  # httpx does this; raises on non-ASCII str
        self.sent_cursors.append(headers.get("CMR-Search-After"))
        return self._pages.pop(0) if self._pages else _FakeResp([], None)


_ITEM_A = {"meta": {"provider-id": "LPCLOUD"}, "umm": {"ShortName": "EMITL2ARFL", "Version": "1"}}
_ITEM_B = {"meta": {"provider-id": "LPCLOUD"}, "umm": {"ShortName": "HLSL30", "Version": "2.0"}}
NON_ASCII_CURSOR = b"sortkey_R\xe9flectance_owner_12345"  # 0xE9 = 'é' (latin-1)


def test_search_after_bytes_reads_raw_cursor():
    assert _search_after_bytes(_FakeResp([], NON_ASCII_CURSOR)) == NON_ASCII_CURSOR
    assert _search_after_bytes(_FakeResp([], None)) is None


@pytest.mark.asyncio
async def test_crawl_roundtrips_non_ascii_cursor_without_crashing():
    """The regression: a cursor with non-ASCII bytes fed back as a str crashed
    httpx. Sent as raw bytes it round-trips, so pagination continues."""
    client = _FakeClient([
        _FakeResp([_ITEM_A], NON_ASCII_CURSOR),  # page 1 → hands back a non-ASCII cursor
        _FakeResp([_ITEM_B], None),              # page 2 → no cursor, end
    ])
    rows, skipped = await crawl_cmr_native(client, {"LPCLOUD": 42})

    assert [r["id"] for r in rows] == ["EMITL2ARFL_1", "HLSL30_2.0"]  # both pages crawled
    assert client.sent_cursors[0] is None                            # page 1: no cursor
    assert client.sent_cursors[1] == NON_ASCII_CURSOR                # page 2: raw bytes, not str


@pytest.mark.asyncio
async def test_crawl_keeps_partial_pages_on_failure():
    """An unexpected page failure stops the crawl but keeps what it collected."""

    class _FailSecond:
        def __init__(self):
            self.n = 0

        async def get(self, url, params=None, headers=None, timeout=None):
            self.n += 1
            if self.n == 1:
                return _FakeResp([_ITEM_A], NON_ASCII_CURSOR)
            raise RuntimeError("boom")

    rows, skipped = await crawl_cmr_native(_FailSecond(), {"LPCLOUD": 42})
    assert [r["id"] for r in rows] == ["EMITL2ARFL_1"]  # page 1 preserved, not lost
