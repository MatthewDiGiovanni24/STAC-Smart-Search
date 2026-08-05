export type ProviderStatus = 'ok' | 'timeout' | 'error';

export interface Asset {
  href?: string;
  type?: string;
  title?: string;
  roles?: string[];
}

export interface Item {
  id: string;
  collection: string | null;
  collection_title?: string | null;
  catalog_source: string;
  datetime: string | null;
  bbox: number[] | null;
  relevance_score: number | null;
  cloud_cover: number | null;
  platform: string | null;
  assets: Record<string, Asset>;
  properties?: {
    title?: string;
    description?: string;
    match_type?: 'exact' | 'semantic';
  };
}

export interface Meta {
  ranked_ids: string[];
  sources: Record<string, ProviderStatus>;
  total: number;
  query_time_ms: number;
  registry_warm: boolean;
}

export interface SearchPayload {
  bbox: [number, number, number, number];
  datetime: string;
  text?: string;
  limit?: number;
}
