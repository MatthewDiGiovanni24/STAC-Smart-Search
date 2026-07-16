import asyncpg
from datetime import datetime
from typing import List, Optional, Dict, Any
from app.schemas.collection import CollectionMetadata

def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    if date_str.endswith('Z'):
        date_str = date_str[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None

async def upsert_collection(pool: asyncpg.Pool, collection: CollectionMetadata):
    """
    Inserts a new collection into the database, or updates it if it already exists.
    """
    min_x, min_y, max_x, max_y = None, None, None, None
    if collection.spatial_extent and len(collection.spatial_extent) >= 4:
        min_x = collection.spatial_extent[0]
        min_y = collection.spatial_extent[1]
        max_x = collection.spatial_extent[2]
        max_y = collection.spatial_extent[3]

    start_time, end_time = None, None
    if collection.temporal_extent and len(collection.temporal_extent) >= 2:
        start_time = parse_date(collection.temporal_extent[0])
        end_time = parse_date(collection.temporal_extent[1])
    
    embedding_str = f"[{','.join(map(str, collection.embedding))}]" if collection.embedding else None

    query = """
        INSERT INTO collections (
            id, provider_id, title, description, 
            min_x, min_y, max_x, max_y, 
            start_time, end_time, embedding
        ) VALUES (
            $1, $2, $3, $4, 
            $5, $6, $7, $8, 
            $9, $10, $11
        )
        ON CONFLICT (id, provider_id) DO UPDATE SET
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            min_x = EXCLUDED.min_x,
            min_y = EXCLUDED.min_y,
            max_x = EXCLUDED.max_x,
            max_y = EXCLUDED.max_y,
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            embedding = EXCLUDED.embedding;
    """

    async with pool.acquire() as conn:
        await conn.execute(
            query,
            collection.id, collection.provider_id, collection.title, collection.description,
            min_x, min_y, max_x, max_y, start_time, end_time, embedding_str
        )

async def get_candidate_collections(
    pool: Any,
    bbox: Optional[List[float]] = None,
    start_time: Optional[str] = None, 
    end_time: Optional[str] = None,
    search_embedding: Optional[List[float]] = None,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """
    Queries the database to find which collections overlap with the search parameters,
    optionally sorting by semantic similarity.
    """
    
    query = "SELECT provider_id, id FROM collections WHERE true"
    args = []
    
    # Spatial Overlap Logic
    if bbox and len(bbox) == 4:
        search_min_x, search_min_y, search_max_x, search_max_y = bbox
        query += f""" 
            AND (
                min_x IS NULL OR 
                (min_x <= ${len(args)+1} AND max_x >= ${len(args)+2} AND 
                 min_y <= ${len(args)+3} AND max_y >= ${len(args)+4})
            )
        """
        args.extend([search_max_x, search_min_x, search_max_y, search_min_y])
        
    # Temporal Overlap Logic
    if start_time and end_time:
        parsed_start = parse_date(start_time)
        parsed_end = parse_date(end_time)
        query += f"""
            AND (start_time IS NULL OR start_time <= ${len(args)+1}::timestamptz)
            AND (end_time IS NULL OR end_time >= ${len(args)+2}::timestamptz)
        """
        args.extend([parsed_end, parsed_start])

    # Semantic Similarity Logic
    if search_embedding:
        embedding_str = f"[{','.join(map(str, search_embedding))}]"
        query += f" ORDER BY embedding <=> ${len(args)+1}::vector ASC"
        args.append(embedding_str)
        
    # Limit the results 
    query += f" LIMIT ${len(args)+1}"
    args.append(limit)

    # Execute
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
        
    return [{"provider_id": row["provider_id"], "id": row["id"]} for row in rows]