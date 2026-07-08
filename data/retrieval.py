import requests
import json
import time
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

# get all endpoints
def get_stac_apis():
    url = "https://stacindex.org/api/catalogs?type=api"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()

# get collections from endpoints
def get_collections_from_api(api_url: str):
    try:
        url = api_url.rstrip("/") + "/collections"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data.get("collections", [])
    except Exception as e:
        print(f"Failed to fetch from {api_url}: {e}")
        return []

# get metadata descriptions
def extract_metadata(collection: dict, source_api: str) -> dict:
    name = collection.get("title") or collection.get("id", "Unknown")
    description = collection.get("description", "")
    keywords = ", ".join(collection.get("keywords", []))
    
    extent = collection.get("extent", {})
    spatial = extent.get("spatial", {}).get("bbox", [[]])[0]
    temporal = extent.get("temporal", {}).get("interval", [[]])[0]
    
    spatial_str = f"Spatial extent: {spatial}" if spatial else ""
    temporal_str = f"Temporal extent: {temporal[0]} to {temporal[1]}" if temporal else ""

    text_for_embedding = " | ".join(filter(None, [
        f"Name: {name}",
        f"Description: {description}",
        f"Keywords: {keywords}",
        spatial_str,
        temporal_str,
    ]))

    return {
        "id": collection.get("id"),
        "name": name,
        "description": description,
        "keywords": keywords,
        "spatial_extent": spatial,
        "temporal_extent": temporal,
        "source_api": source_api,
        "text_for_embedding": text_for_embedding,
    }

# harvester function to get collections from APIs
def harvest_all_collections():
    print("Fetching STAC API list from STAC Index...")
    apis = get_stac_apis()
    print(f"Found {len(apis)} APIs\n")

    all_collections = []

    for api in apis:
        api_url = api.get("url")
        api_title = api.get("title", api_url)
        if not api_url:
            continue

        print(f"Harvesting: {api_title} ({api_url})")
        collections = get_collections_from_api(api_url)
        print(f"Found {len(collections)} collections")

        for col in collections:
            metadata = extract_metadata(col, source_api=api_url)
            all_collections.append(metadata)

        time.sleep(0.25)

    print(f"\nTotal collections harvested: {len(all_collections)}")
    return all_collections

# embeddings
def load_into_qdrant(collections: list):
    print("\nGenerating embeddings...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    
    texts = [c["text_for_embedding"] for c in collections]
    embeddings = model.encode(texts, show_progress_bar=True)

    print("Loading into Qdrant...")
    client = QdrantClient(host="localhost", port=6333)

    client.recreate_collection(
        collection_name="stac_collections",
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )

    points = [
        PointStruct(
            id=i,
            vector=embeddings[i].tolist(),
            payload=collections[i]
        )
        for i in range(len(collections))
    ]

    client.upsert(collection_name="stac_collections", points=points)
    print(f"Loaded {len(points)} collections into Qdrant!")


if __name__ == "__main__":
    collections = harvest_all_collections()

    with open("collections_catalog.json", "w") as f:
        json.dump(collections, f, indent=2)

    load_into_qdrant(collections)