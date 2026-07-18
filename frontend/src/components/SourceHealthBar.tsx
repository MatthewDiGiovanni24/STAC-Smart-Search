import type { ProviderStatus } from '../types';

type Status = ProviderStatus | 'pending';

interface Props {
  // Provider name -> status. During streaming this is built from arriving
  // items (all "ok"); after the meta event it's the authoritative health map.
  sources: Record<string, Status>;
  streaming: boolean;
}

const LABEL: Record<Status, string> = {
  pending: 'pending',
  ok: 'ok',
  timeout: 'timeout',
  error: 'error',
};

export function SourceHealthBar({ sources, streaming }: Props) {
  const entries = Object.entries(sources);

  return (
    <div className="health-bar">
      <span className="health-bar__label">Catalogs</span>
      {entries.length === 0 && (
        <span className="health-pill health-pill--pending">
          {streaming ? 'querying…' : 'none yet'}
        </span>
      )}
      {entries.map(([name, status]) => (
        <span key={name} className={`health-pill health-pill--${status}`} title={LABEL[status]}>
          <span className="dot" />
          {name}
        </span>
      ))}
    </div>
  );
}
