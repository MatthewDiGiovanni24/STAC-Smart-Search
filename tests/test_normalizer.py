"""Unit tests for app.services.normalizer.normalize_item.

normalize_item is a pure function (dict -> NormalizedSTACItem), so these tests
need no mocking or network — they feed hand-built STAC features shaped like the
ones the real catalogs return and assert on the normalized output.
"""

from app.schemas.search import NormalizedSTACItem
from app.services.normalizer import normalize_item


def test_normal_item_all_fields_present():
    """A fully-populated item maps every field through correctly."""
    feature = {
        "id": "S2A_123",
        "collection": "sentinel-2-l2a",
        "geometry": {"type": "Point", "coordinates": [10.0, 20.0]},
        "bbox": [9.0, 19.0, 11.0, 21.0],
        "assets": {"thumbnail": {"href": "https://example.com/thumb.png"}},
        "properties": {
            "datetime": "2024-05-01T10:00:00Z",
            "eo:cloud_cover": 12.5,
            "platform": "sentinel-2a",
            "instruments": ["msi"],
            "sat:constellation": "sentinel-2",
            "eo:bands": [{"name": "B01"}, {"name": "B02"}],
        },
    }

    item = normalize_item(feature, "earth_search")

    assert isinstance(item, NormalizedSTACItem)
    assert item.id == "S2A_123"
    assert item.collection == "sentinel-2-l2a"
    assert item.catalog_source == "earth_search"
    assert item.datetime == "2024-05-01T10:00:00Z"
    assert item.bbox == [9.0, 19.0, 11.0, 21.0]
    assert item.cloud_cover == 12.5
    assert item.platform == "sentinel-2a"
    assert item.instruments == ["msi"]
    assert item.constellation == "sentinel-2"
    assert item.bands == [{"name": "B01"}, {"name": "B02"}]
    # Full raw properties are retained, and the original feature is preserved.
    assert item.properties == feature["properties"]
    assert item.raw_source == feature


def test_null_datetime_falls_back_to_start_datetime():
    """datetime: null with a start_datetime present uses start_datetime."""
    feature = {
        "id": "x",
        "collection": "c",
        "properties": {
            "datetime": None,
            "start_datetime": "2024-01-01T00:00:00Z",
            "end_datetime": "2024-01-31T23:59:59Z",
        },
    }

    item = normalize_item(feature, "cmr")

    assert item.datetime == "2024-01-01T00:00:00Z"


def test_all_datetimes_missing_yields_none():
    """No datetime, start_datetime, or end_datetime -> None (no crash)."""
    feature = {"id": "x", "collection": "c", "properties": {}}

    item = normalize_item(feature, "cmr")

    assert item.datetime is None


def test_bbox_derived_from_polygon_geometry():
    """Missing bbox is derived as min/max over polygon coordinates."""
    feature = {
        "id": "poly",
        "collection": "c",
        # No top-level bbox.
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-10.0, -5.0],
                    [-10.0, 5.0],
                    [10.0, 5.0],
                    [10.0, -5.0],
                    [-10.0, -5.0],
                ]
            ],
        },
        "properties": {"datetime": "2024-05-01T10:00:00Z"},
    }

    item = normalize_item(feature, "planetary_computer")

    assert item.bbox == [-10.0, -5.0, 10.0, 5.0]


def test_bbox_derived_from_point_geometry():
    """Missing bbox for a Point becomes [lon, lat, lon, lat]."""
    feature = {
        "id": "pt",
        "collection": "c",
        "geometry": {"type": "Point", "coordinates": [30.0, 40.0]},
        "properties": {"datetime": "2024-05-01T10:00:00Z"},
    }

    item = normalize_item(feature, "cmr")

    assert item.bbox == [30.0, 40.0, 30.0, 40.0]


def test_bands_present_planetary_computer_style():
    """eo:bands present -> bands is the list of band dicts."""
    feature = {
        "id": "pc",
        "collection": "c",
        "properties": {
            "datetime": "2024-05-01T10:00:00Z",
            "eo:bands": [
                {"name": "B04", "common_name": "red"},
                {"name": "B03", "common_name": "green"},
            ],
        },
    }

    item = normalize_item(feature, "planetary_computer")

    assert item.bands == [
        {"name": "B04", "common_name": "red"},
        {"name": "B03", "common_name": "green"},
    ]


def test_bands_absent_cmr_style_is_none():
    """No eo:bands (typical CMR item) -> bands is None, not []."""
    feature = {
        "id": "cmr1",
        "collection": "c",
        "properties": {"datetime": "2024-05-01T10:00:00Z"},
    }

    item = normalize_item(feature, "cmr")

    assert item.bands is None


def test_instruments_string_normalized_to_list():
    """A bare-string instruments value is normalized to a single-element list."""
    feature = {
        "id": "inst",
        "collection": "c",
        "properties": {
            "datetime": "2024-05-01T10:00:00Z",
            "instruments": "msi",
        },
    }

    item = normalize_item(feature, "earth_search")

    assert item.instruments == ["msi"]


def test_instruments_from_sat_instrument_fallback():
    """instruments falls back to sat:instrument when instruments is absent."""
    feature = {
        "id": "inst2",
        "collection": "c",
        "properties": {
            "datetime": "2024-05-01T10:00:00Z",
            "sat:instrument": "OLI",
        },
    }

    item = normalize_item(feature, "cmr")

    assert item.instruments == ["OLI"]


def test_platform_falls_back_to_sat_platform():
    """platform is read from sat:platform when the plain key is absent."""
    feature = {
        "id": "p",
        "collection": "c",
        "properties": {
            "datetime": "2024-05-01T10:00:00Z",
            "sat:platform": "landsat-8",
        },
    }

    item = normalize_item(feature, "cmr")

    assert item.platform == "landsat-8"


def test_no_geometry_and_no_bbox_are_none():
    """An item with neither geometry nor bbox yields None for both, no crash."""
    feature = {
        "id": "bare",
        "collection": "c",
        "properties": {"datetime": "2024-05-01T10:00:00Z"},
    }

    item = normalize_item(feature, "cmr")

    assert item.bbox is None
    assert item.geometry is None


def test_collection_falls_back_to_properties_then_none():
    """collection resolves feature -> properties -> None."""
    from_props = normalize_item(
        {"id": "a", "properties": {"collection": "coll-from-props"}}, "cmr"
    )
    assert from_props.collection == "coll-from-props"

    neither = normalize_item({"id": "a", "properties": {}}, "cmr")
    assert neither.collection is None


def test_malformed_input_does_not_raise():
    """Deeply malformed features must not raise and must still normalize."""
    for bad in ({}, {"geometry": "nope", "bbox": "nope", "properties": None}):
        item = normalize_item(bad, "cmr")
        assert isinstance(item, NormalizedSTACItem)
        assert item.bbox is None
        assert item.geometry is None
        assert item.catalog_source == "cmr"
