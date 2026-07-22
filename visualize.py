import psycopg2
import pandas as pd
import numpy as np
from sklearn.decomposition import PCA
import plotly.express as px
import json

DB_URL = "postgresql://postgres:postgres@localhost:5432/stac"

def fetch_data():
    print("Connecting to database...")
    conn = psycopg2.connect(DB_URL)
    
    query = """
        SELECT title, id, provider_id, embedding::text 
        FROM collections 
        WHERE embedding IS NOT NULL;
    """
    
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def main():
    df = fetch_data()
    
    if df.empty:
        print("No embeddings found in the database!")
        return

    print(f"Loaded {len(df)} collections with embeddings.")
    
    print("Parsing vectors...")
    df['embedding'] = df['embedding'].apply(json.loads)
    
    matrix = np.vstack(df['embedding'].values)
    
    print(f"Reducing dimensions from {matrix.shape[1]} to 3 using PCA...")
    pca = PCA(n_components=3)
    components = pca.fit_transform(matrix)
    
    df['x'] = components[:, 0]
    df['y'] = components[:, 1]
    df['z'] = components[:, 2]
    
    df['display_name'] = df['title'].fillna(df['id'])

    print("Generating 3D plot...")
    fig = px.scatter_3d(
        df,
        x='x', y='y', z='z',
        color='provider_id',
        hover_name='display_name',
        hover_data={'x': False, 'y': False, 'z': False, 'provider_id': False},
        title="STAC Collections Semantic Embedding Space",
        opacity=0.7
    )
    
    fig.update_traces(marker=dict(size=4))
    
    fig.show()

if __name__ == "__main__":
    main()