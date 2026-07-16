import pytest
from unittest.mock import AsyncMock, MagicMock
from app.schemas.collection import CollectionMetadata
from app.services.registry import upsert_collection
from app.services.registry import get_candidate_collections

@pytest.mark.asyncio
async def test_upsert_collection_extracts_and_executes():
    """
    Test that upsert_collection correctly extracts lists into individual variables
    and passes them to the database execute function.
    """
    # Create dummy collection data
    dummy_collection = CollectionMetadata(
        id="test-col-1",
        provider_id=99,
        title="Test Collection",
        description="A test description",
        spatial_extent=[-180.0, -90.0, 180.0, 90.0],
        temporal_extent=["2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z"],
        embedding=[0.1, 0.2, 0.3]
    )

    # Mock the asyncpg database pool and connection
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    # Mock the 'async with pool.acquire() as conn:' context manager
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Run our function
    await upsert_collection(mock_pool, dummy_collection)

    mock_conn.execute.assert_called_once()
    
    # Grab the arguments passed to conn.execute()
    call_args = mock_conn.execute.call_args[0]
    
    # query is call_args[0]
    assert call_args[1] == "test-col-1"              # id
    assert call_args[2] == 99                        # provider_id
    assert call_args[3] == "Test Collection"         # title
    assert call_args[4] == "A test description"      # description
    assert call_args[5] == -180.0                    # min_x
    assert call_args[6] == -90.0                     # min_y
    assert call_args[7] == 180.0                     # max_x
    assert call_args[8] == 90.0                      # max_y
    assert call_args[9] == "2020-01-01T00:00:00Z"    # start_time
    assert call_args[10] == "2021-01-01T00:00:00Z"   # end_time
    assert call_args[11] == "[0.1,0.2,0.3]"          # embedding string

@pytest.mark.asyncio
async def test_get_candidate_collections_builds_query():
    """
    Test that the function correctly builds the SQL query with bbox and time.
    """
    # Mock the asyncpg pool
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"provider_id": 1, "id": "test-collection"}]
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    # Run the function with a bbox and time
    result = await get_candidate_collections(
        pool=mock_pool,
        bbox=[10.0, 20.0, 30.0, 40.0],
        start_time="2020-01-01T00:00:00Z",
        end_time="2020-12-31T23:59:59Z"
    )

    # Assert the function returned the formatted dictionary
    assert result == [{"provider_id": 1, "id": "test-collection"}]

    # Check that the SQL query included our parameters
    mock_conn.fetch.assert_called_once()
    call_args = mock_conn.fetch.call_args[0]
    
    query = call_args[0]
    
    # Check that the spatial and temporal logic got added to the SQL string
    assert "min_x <=" in query
    assert "start_time <=" in query
    
    # Check that the 6 variables (4 bbox + 2 time) were passed correctly
    assert call_args[1] == 30.0  # max_x
    assert call_args[2] == 10.0  # min_x
    assert call_args[5] == "2020-12-31T23:59:59Z" # end_time