"""STAC feature normalization.

The single public entry point, :func:`normalize_item`, converts a raw STAC
feature (a plain ``dict`` exactly as returned by a catalog) into the unified
:class:`~app.schemas.search.NormalizedSTACItem`.

Catalogs disagree on where fields live (``platform`` vs ``sat:platform``),
whether ``bbox`` is present, how ``datetime`` is expressed (a single value vs a
``start_datetime``/``end_datetime`` interval), and which vendor extensions they
apply. This module absorbs those differences.

Design contract: ``normalize_item`` is a pure function that MUST NOT raise on
malformed input. Every extraction is guarded and falls back to ``None`` (or an
empty container) rather than propagating an error, so a single bad feature can
never take down a whole fan-out response.
"""

from datetime import datetime
from typing import Any

from app.schemas.search import NormalizedSTACItem


def normalize_item(raw_feature: dict, catalog_source: str) -> NormalizedSTACItem:
    """Normalize a raw STAC feature into a :class:`NormalizedSTACItem`.

    Args:
        raw_feature: A STAC feature dict as returned by a catalog's ``/search``.
        catalog_source: The source identifier for the originating catalog
            (e.g. ``"cmr"``, ``"planetary_computer"``, ``"earth_search"``).

    Returns:
        A fully normalized item. Never raises for malformed input; unknown or
        missing fields become ``None`` / empty containers.
    """
    feature = raw_feature if isinstance(raw_feature, dict) else {}
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    return NormalizedSTACItem(
        id=_extract_id(feature),
        collection=_extract_collection(feature, properties),
        catalog_source=catalog_source,
        geometry=_extract_geometry(feature),
        bbox=_extract_bbox(feature),
        datetime=_extract_datetime(properties),
        properties=properties,
        assets=_extract_assets(feature),
        cloud_cover=_extract_cloud_cover(properties),
        platform=_extract_platform(properties),
        instruments=_extract_instruments(properties),
        constellation=_extract_constellation(properties),
        bands=_extract_bands(properties),
        relevance_score=None,
        raw_source=feature,
    )


# --- Identity / provenance -------------------------------------------------


def _extract_id(feature: dict) -> str | None:
    """Return the feature id as a string, or None if absent."""
    value = feature.get("id")
    if value is None:
        return None
    return str(value)


def _extract_collection(feature: dict, properties: dict) -> str | None:
    """Return the collection id from the feature, falling back to properties."""
    for candidate in (feature.get("collection"), properties.get("collection")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _extract_geometry(feature: dict) -> dict[str, Any] | None:
    """Return the GeoJSON geometry dict, or None if absent/malformed."""
    geometry = feature.get("geometry")
    return geometry if isinstance(geometry, dict) else None


def _extract_assets(feature: dict) -> dict[str, Any]:
    """Return the assets mapping, or an empty dict if absent/malformed."""
    assets = feature.get("assets")
    return assets if isinstance(assets, dict) else {}


# --- Datetime --------------------------------------------------------------


def _extract_datetime(properties: dict) -> str | None:
    """Resolve the item datetime.

    STAC allows ``properties.datetime`` to be ``null`` for items that instead
    carry a ``start_datetime``/``end_datetime`` interval. Try each in order and
    return the first that is a non-empty, parseable ISO 8601 string. Return
    None if none qualify — never raise, never guess a value.
    """
    for key in ("datetime", "start_datetime", "end_datetime"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip() and _is_iso8601(value):
            return value
    return None


def _is_iso8601(value: str) -> bool:
    """Best-effort check that ``value`` parses as an ISO 8601 datetime."""
    candidate = value.strip()
    # Python's fromisoformat only accepts a trailing 'Z' from 3.11+, but be
    # explicit so the intent is clear and robust across patch versions.
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        datetime.fromisoformat(candidate)
        return True
    except (ValueError, TypeError):
        return False


# --- Bounding box ----------------------------------------------------------


def _extract_bbox(feature: dict) -> list[float] | None:
    """Return a 4-element bbox, deriving it from geometry when absent.

    Prefers the top-level ``bbox`` when it is a well-formed 4-element numeric
    list. Otherwise derives one from the geometry (Point or Polygon). Returns
    None for anything else (missing geometry, unsupported geometry type, etc.).
    """
    bbox = feature.get("bbox")
    coerced = _coerce_bbox4(bbox)
    if coerced is not None:
        return coerced
    return _bbox_from_geometry(feature.get("geometry"))


def _coerce_bbox4(bbox: Any) -> list[float] | None:
    """Return ``bbox`` as a list of 4 floats, or None if not a valid 4-tuple."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        return [float(x) for x in bbox]
    except (TypeError, ValueError):
        return None


def _bbox_from_geometry(geometry: Any) -> list[float] | None:
    """Derive a bbox from a GeoJSON geometry (Point or Polygon only)."""
    if not isinstance(geometry, dict):
        return None

    geom_type = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if geom_type == "Point":
        point = _as_lonlat(coordinates)
        if point is None:
            return None
        lon, lat = point
        return [lon, lat, lon, lat]

    if geom_type == "Polygon":
        points = _flatten_positions(coordinates)
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return [min(xs), min(ys), max(xs), max(ys)]

    # Other geometry types (LineString, MultiPolygon, ...) are out of scope.
    return None


def _as_lonlat(coordinates: Any) -> tuple[float, float] | None:
    """Interpret ``coordinates`` as a single ``[lon, lat]`` position."""
    if (
        isinstance(coordinates, (list, tuple))
        and len(coordinates) >= 2
        and _is_number(coordinates[0])
        and _is_number(coordinates[1])
    ):
        return float(coordinates[0]), float(coordinates[1])
    return None


def _flatten_positions(node: Any) -> list[tuple[float, float]]:
    """Recursively collect every ``[lon, lat]`` position under ``node``.

    Handles arbitrary nesting depth (Polygon rings, and defensively deeper),
    ignoring anything that is not a numeric coordinate pair.
    """
    positions: list[tuple[float, float]] = []

    def walk(item: Any) -> None:
        if not isinstance(item, (list, tuple)):
            return
        if (
            len(item) >= 2
            and _is_number(item[0])
            and _is_number(item[1])
            and not any(isinstance(sub, (list, tuple)) for sub in item)
        ):
            positions.append((float(item[0]), float(item[1])))
            return
        for child in item:
            walk(child)

    walk(node)
    return positions


def _is_number(value: Any) -> bool:
    """True for real numbers, excluding bool (a subclass of int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- Vendor / extension fields ---------------------------------------------


def _extract_cloud_cover(properties: dict) -> float | None:
    """Extract ``eo:cloud_cover`` as a float, or None."""
    value = properties.get("eo:cloud_cover")
    if _is_number(value):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _extract_platform(properties: dict) -> str | None:
    """Extract the platform from ``platform`` or ``sat:platform``."""
    for key in ("platform", "sat:platform"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_instruments(properties: dict) -> list[str] | None:
    """Extract instruments as a list of strings.

    Reads ``instruments`` then falls back to ``sat:instrument``. Some catalogs
    return a bare string rather than a list; normalize both to ``list[str]``.
    Returns None when nothing usable is present.
    """
    value = properties.get("instruments") or properties.get("sat:instrument")
    if not value:
        return None
    if isinstance(value, str):
        return [value] if value.strip() else None
    if isinstance(value, (list, tuple)):
        result = [str(v) for v in value if v is not None and str(v).strip()]
        return result or None
    return None


def _extract_constellation(properties: dict) -> str | None:
    """Extract the constellation from ``sat:constellation``."""
    value = properties.get("sat:constellation")
    return value if isinstance(value, str) and value.strip() else None


def _extract_bands(properties: dict) -> list[dict] | None:
    """Extract ``eo:bands`` as a list of dicts, or None when absent.

    Present on Planetary Computer and Earth Search items; absent on most CMR
    items, which must yield None (not an empty list).
    """
    value = properties.get("eo:bands")
    if isinstance(value, (list, tuple)):
        bands = [b for b in value if isinstance(b, dict)]
        return bands or None
    return None
