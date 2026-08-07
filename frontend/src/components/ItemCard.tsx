import type { Item } from '../types';

interface Props {
  item: Item;
  registerRef?: (el: HTMLElement | null) => void;
}

// Pick a link to open on click: prefer a self link, then a browse/thumbnail
// image, then any asset with an href.
function itemHref(item: Item): string | undefined {
  const a = item.assets || {};
  const preferred = a.self?.href || a.browse?.href || a.thumbnail_0?.href || a.thumbnail?.href;
  if (preferred) return preferred;
  return Object.values(a).find((asset) => asset?.href)?.href;
}

function formatDate(dt: string | null): string {
  if (!dt) return '—';
  const d = new Date(dt);
  return Number.isNaN(d.getTime()) ? dt : d.toISOString().slice(0, 10);
}

// Factual label for the lexical tier the query matched (not a score). Falls
// back to the coarse match_type for backward compatibility.
const TIER_BADGE: Record<string, { label: string; title: string }> = {
  exact: { label: 'Exact match', title: 'Query equals this collection’s id or title' },
  prefix: { label: 'Name match', title: 'This collection’s id or title starts with your query' },
  substring: { label: 'Partial match', title: 'Your query appears within the id or title' },
};

function tierBadge(item: Item): { label: string; title: string } | null {
  const tier = item.properties?.match_tier
    ?? (item.properties?.match_type === 'exact' ? 'substring' : undefined);
  return tier ? TIER_BADGE[tier] ?? null : null;
}

export function ItemCard({ item, registerRef }: Props) {
  const href = itemHref(item);
  const score = item.relevance_score;
  const pct = score != null ? Math.round(Math.max(0, Math.min(1, score)) * 100) : null;
  const badge = tierBadge(item);

  return (
    <article
      ref={registerRef}
      className={`card${href ? ' card--clickable' : ''}`}
      onClick={() => href && window.open(href, '_blank', 'noopener,noreferrer')}
    >
      <div className="card__head">
        <div className="card__tags">
          <span className={`badge badge--${item.catalog_source}`}>{item.catalog_source}</span>
          {badge && (
            <span
              className={`badge badge--tier badge--tier-${item.properties?.match_tier ?? 'exact'}`}
              title={badge.title}
            >
              {badge.label}
            </span>
          )}
        </div>
        {pct != null && <span className="card__score-num">{pct}%</span>}
      </div>

      <h3 className="card__collection">
        {item.collection_title || item.collection || '(no collection)'}
      </h3>
      {/* {item.collection_title && item.collection_title !== item.collection && (
        <p className="card__collection-id">{item.collection}</p>
      )} */}
      <p className="card__id" title={item.id}>
        {item.properties?.title || item.id}
      </p>

      {pct != null && (
        <div className="score-bar" aria-label={`relevance ${pct}%`}>
          <div className="score-bar__fill" style={{ width: `${pct}%` }} />
        </div>
      )}

      <dl className="card__meta">
        <div><dt>date</dt><dd>{formatDate(item.datetime)}</dd></div>
        {item.cloud_cover != null && (
          <div><dt>cloud</dt><dd>{Math.round(item.cloud_cover)}%</dd></div>
        )}
        {item.platform && <div><dt>platform</dt><dd>{item.platform}</dd></div>}
      </dl>
    </article>
  );
}
