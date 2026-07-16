import json
from sentence_transformers import SentenceTransformer

def main():
    print("Loading model...")
    model = SentenceTransformer('all-MiniLM-L6-v2')

    print("Loading JSON data...")
    with open('data/collections.json', 'r') as f:
        data = json.load(f)

    print(f"Generating embeddings for {len(data)} collections...")
    for item in data:
        text = item.get('text_for_embedding', '')
        if text:
            vector = model.encode(text).tolist()
            item['embedding'] = vector
        else:
            item['embedding'] = None

    print("Saving to new JSON file...")
    with open('data/collections_with_embeddings.json', 'w') as f:
        json.dump(data, f, indent=2)
        
    print("Done!")

if __name__ == "__main__":
    main()