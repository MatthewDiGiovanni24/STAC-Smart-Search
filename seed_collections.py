import asyncio
import json
import asyncpg
import os
from app.schemas.collection import CollectionMetadata
from app.services.registry import upsert_collection

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stac")

async def main():
    print(f"Connecting to database...")
    pool = await asyncpg.create_pool(DATABASE_URL)

    # Map existing providers (stripping trailing slashes for safety)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, base_url FROM providers")
        url_to_id = {row['base_url'].rstrip('/'): row['id'] for row in rows}

    print("Loading JSON file...")
    with open('data/collections_with_embeddings.json', 'r') as f:
        data = json.load(f)

    print(f"Inserting {len(data)} collections into the database...")
    inserted_count = 0
    
    for item in data:
        provider_url = item.get('source_api', '').rstrip('/')
        if not provider_url:
            continue

        pid = url_to_id.get(provider_url)
        
        # auto add
        if not pid:
            print(f"Auto-adding missing provider: {provider_url}")
            async with pool.acquire() as conn:
                pid = await conn.fetchval("""
                    INSERT INTO providers (name, base_url, source) 
                    VALUES ($1, $2, $3) RETURNING id
                """, provider_url, provider_url, "json_seed")
            # Save it in dictionary
            url_to_id[provider_url] = pid

        # Build the Pydantic model
        meta = CollectionMetadata(
            id=item['id'],
            provider_id=pid,
            title=item.get('name'),
            description=item.get('description'),
            spatial_extent=item.get('spatial_extent'),
            temporal_extent=item.get('temporal_extent'),
            embedding=item.get('embedding')
        )
        
        # Save to DB
        await upsert_collection(pool, meta)
        inserted_count += 1

    print(f"Successfully seeded {inserted_count} collections!")

if __name__ == '__main__':
    asyncio.run(main())