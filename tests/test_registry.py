import pytest
from unittest.mock import AsyncMock, MagicMock

from app.schemas.collection import CollectionMetadata
from app.services.registry import get_candidate_collections, parse_date, upsert_collection


@pytest.mark.asyncio
async def test_upsert_collection_extracts_and_executes():
    """upsert_collection unpacks extents, parses dates, and passes params in order.

    Dates are passed as parsed ``datetime`` objects (the column is timestamptz),
    not raw strings, and the embedding is a pgvector text literal.
    """
    dummy_collection = CollectionMetadata(
        id="test-col-1",
        provider_id=99,
        title="Test Collection",
        description="A test description",
        spatial_extent=[-180.0, -90.0, 180.0, 90.0],
        temporal_extent=["2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z"],
        embedding=[0.1, 0.2, 0.3],
    )

    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    await upsert_collection(mock_pool, dummy_collection)

    mock_conn.execute.assert_called_once()
    call_args = mock_conn.execute.call_args[0]

    # call_args[0] is the SQL; params follow in $1..$12 order.
    assert call_args[1] == "test-col-1"                      # id
    assert call_args[2] == 99                                # provider_id
    assert call_args[3] == "Test Collection"                 # title
    assert call_args[4] == "A test description"              # description
    assert call_args[5] == -180.0                            # min_x
    assert call_args[6] == -90.0                             # min_y
    assert call_args[7] == 180.0                             # max_x
    assert call_args[8] == 90.0                              # max_y
    assert call_args[9] == parse_date("2020-01-01T00:00:00Z")  # start_time (parsed)
    assert call_args[10] == parse_date("2021-01-01T00:00:00Z")  # end_time (parsed)
    assert call_args[11] == "[0.1,0.2,0.3]"                  # embedding literal
    assert call_args[12] is None                             # description_hash (single-upsert path)


@pytest.mark.asyncio
async def test_get_candidate_collections_builds_query():
    """The pre-filter query adds spatial + temporal clauses and passes params in order."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"provider_id": 1, "id": "test-collection", "title": "Test", "is_exact": False}
    ]
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    result = await get_candidate_collections(
        pool=mock_pool,
        bbox=[10.0, 20.0, 30.0, 40.0],
        start_time="2020-01-01T00:00:00Z",
        end_time="2020-12-31T23:59:59Z",
    )

    assert result == [
        {"provider_id": 1, "id": "test-collection", "title": "Test", "is_exact": False}
    ]

    mock_conn.fetch.assert_called_once()
    call_args = mock_conn.fetch.call_args[0]
    query = call_args[0]

    assert "min_x <=" in query
    assert "start_time <=" in query
    # No embedding/text: nothing can be a lexical match.
    assert "false AS is_exact" in query

    # Param order: [max_x, min_x, max_y, min_y, parsed_end, parsed_start, limit]
    assert call_args[1] == 30.0                                 # search max_x
    assert call_args[2] == 10.0                                 # search min_x
    assert call_args[3] == 40.0                                 # search max_y
    assert call_args[4] == 20.0                                 # search min_y
    assert call_args[5] == parse_date("2020-12-31T23:59:59Z")   # parsed end
    assert call_args[6] == parse_date("2020-01-01T00:00:00Z")   # parsed start


@pytest.mark.asyncio
async def test_candidate_query_labels_exact_matches_with_the_expression_it_orders_by():
    """``is_exact`` comes back from SQL, computed by the same lexical expression
    that admits and orders the row — callers must not re-derive it."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"provider_id": 1, "id": "sentinel-2-l2a", "title": "Sentinel-2", "is_exact": True},
        {"provider_id": 2, "id": "landsat-8", "title": "Landsat 8", "is_exact": False},
    ]
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    result = await get_candidate_collections(
        pool=mock_pool, text="sentinel", search_embedding=[0.1, 0.2, 0.3]
    )

    assert [r["is_exact"] for r in result] == [True, False]
    assert result[0]["title"] == "Sentinel-2"

    query = mock_conn.fetch.call_args[0][0]
    # No bbox/temporal here, so the vector is $1 and the text pattern is $2.
    exact_expr = "(id ILIKE $2 OR title ILIKE $2)"
    assert f"{exact_expr} AS is_exact" in query      # labels
    assert f"OR {exact_expr}" in query               # admits
    assert f"ORDER BY {exact_expr} DESC" in query    # orders
    assert mock_conn.fetch.call_args[0][2] == "%sentinel%"
