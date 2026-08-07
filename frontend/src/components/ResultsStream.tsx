import { useEffect, useMemo, useRef, useState } from 'react';
import type { Item, Meta, ProviderStatus, SearchPayload } from '../types';
import { streamSearch } from '../lib/sse';
import { useFlip } from '../lib/useFlip';
import { ItemCard } from './ItemCard';
import { SourceHealthBar } from './SourceHealthBar';

const API_BASE = import.meta.env.VITE_API_URL ?? '';

type Phase = 'idle' | 'streaming' | 'ranked' | 'error';

/**
 * Exact matches ahead of semantic, preserving arrival order within each lane.
 * Applied as items stream in (before `meta`) so a fast semantic catalog can't
 * briefly sit on top; `meta`'s `ranked_ids` then refines within-lane by score.
 * Array.prototype.sort is stable, so equal keys keep arrival order.
 */
function blockedArrival(items: Item[]): Item[] {
  return [...items].sort(
    (a, b) =>
      Number(a.properties?.match_type !== 'exact') -
      Number(b.properties?.match_type !== 'exact'),
  );
}

interface Props {
  query: SearchPayload | null;
  onSettled?: () => void;
}

export function ResultsStream({ query, onSettled }: Props) {
  const [items, setItems] = useState<Item[]>([]);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const byId = useRef<Map<string, Item>>(new Map());
  const register = useFlip();

  useEffect(() => {
    if (!query) return;
    const controller = new AbortController();
    byId.current = new Map();
    setItems([]);
    setMeta(null);
    setErrorMsg(null);
    setPhase('streaming');

    streamSearch(
      API_BASE,
      query,
      {
        onItem: (item) => {
          if (byId.current.has(item.id)) return; // dedupe
          byId.current.set(item.id, item);
          // Exact-first as they arrive (no mid-stream flicker); meta refines later.
          setItems(blockedArrival(Array.from(byId.current.values())));
        },
        onMeta: (m) => {
          setMeta(m);
          // Settle into the authoritative global relevance order.
          const rankedSet = new Set(m.ranked_ids);
          const ordered: Item[] = [];
          for (const id of m.ranked_ids) {
            const it = byId.current.get(id);
            if (it) ordered.push(it);
          }
          for (const it of byId.current.values()) {
            if (!rankedSet.has(it.id)) ordered.push(it); // defensive: keep unranked
          }
          setItems(ordered);
          setPhase('ranked');
          onSettled?.();
        },
      },
      controller.signal,
    ).catch((err: unknown) => {
      if (controller.signal.aborted) return;
      setErrorMsg(err instanceof Error ? err.message : String(err));
      setPhase('error');
      onSettled?.();
    });

    return () => controller.abort();
    // onSettled is stable (useCallback in parent); excluded to avoid re-subscribing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  // Source health: during streaming, derive "ok" from arriving items'
  // catalog_source; after meta, use the authoritative provider-level map.
  const sources = useMemo<Record<string, ProviderStatus | 'ok'>>(() => {
    if (meta) return meta.sources;
    const live: Record<string, 'ok'> = {};
    for (const it of items) live[it.catalog_source] = 'ok';
    return live;
  }, [meta, items]);

  if (phase === 'idle') {
    return <p className="hint">Enter a query and hit Search to stream results.</p>;
  }

  return (
    <section className="results">
      <SourceHealthBar sources={sources} streaming={phase === 'streaming'} />

      <div className="results__status">
        {phase === 'streaming' && <span className="pulse">Streaming results…</span>}
        {phase === 'ranked' && meta && (
          <span>
            {meta.total} result{meta.total === 1 ? '' : 's'} · sorted by relevance ·{' '}
            {Math.round(meta.query_time_ms)} ms
            {!meta.registry_warm && ' · registry still warming'}
          </span>
        )}
        {phase === 'error' && <span className="error-text">Error: {errorMsg}</span>}
      </div>

      {phase !== 'error' && items.length === 0 && (
        <p className="hint">
          {meta
            ? meta.registry_warm
              ? 'No matching items for this query.'
              : 'No results yet — the collection registry is still warming up.'
            : 'Waiting for the first catalog to respond…'}
        </p>
      )}

      <div className="card-grid">
        {items.map((item) => (
          <ItemCard key={item.id} item={item} registerRef={register(item.id)} />
        ))}
      </div>
    </section>
  );
}
