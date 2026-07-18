import { useState } from 'react';
import type { SearchPayload } from '../types';

interface Props {
  onSearch: (payload: SearchPayload) => void;
  disabled?: boolean;
}

// Sensible defaults so the demo is one click: a Louisiana bbox + 2023 + "flood".
const DEFAULTS = {
  minLon: '-93.8',
  minLat: '28.9',
  maxLon: '-88.7',
  maxLat: '33.0',
  start: '2023-01-01',
  end: '2023-12-31',
  text: 'flood inundation',
};

export function SearchForm({ onSearch, disabled }: Props) {
  const [minLon, setMinLon] = useState(DEFAULTS.minLon);
  const [minLat, setMinLat] = useState(DEFAULTS.minLat);
  const [maxLon, setMaxLon] = useState(DEFAULTS.maxLon);
  const [maxLat, setMaxLat] = useState(DEFAULTS.maxLat);
  const [start, setStart] = useState(DEFAULTS.start);
  const [end, setEnd] = useState(DEFAULTS.end);
  const [text, setText] = useState(DEFAULTS.text);
  const [error, setError] = useState<string | null>(null);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const bbox = [minLon, minLat, maxLon, maxLat].map(Number);
    if (bbox.some((n) => Number.isNaN(n))) {
      setError('All four bounding-box values must be valid numbers.');
      return;
    }
    if (!start || !end) {
      setError('Start and end dates are required.');
      return;
    }
    setError(null);
    onSearch({
      bbox: bbox as [number, number, number, number],
      datetime: `${start}T00:00:00Z/${end}T23:59:59Z`,
      text: text.trim() || undefined,
      limit: 20,
    });
  }

  return (
    <form className="search-form" onSubmit={submit}>
      <label className="field field--wide">
        <span>Semantic query</span>
        <input
          type="text"
          value={text}
          placeholder="e.g. flood inundation, wildfire burn scar, glacier"
          onChange={(e) => setText(e.target.value)}
        />
      </label>

      <fieldset className="bbox">
        <legend>Bounding box</legend>
        <label className="field">
          <span>min lon</span>
          <input type="number" step="any" value={minLon} onChange={(e) => setMinLon(e.target.value)} />
        </label>
        <label className="field">
          <span>min lat</span>
          <input type="number" step="any" value={minLat} onChange={(e) => setMinLat(e.target.value)} />
        </label>
        <label className="field">
          <span>max lon</span>
          <input type="number" step="any" value={maxLon} onChange={(e) => setMaxLon(e.target.value)} />
        </label>
        <label className="field">
          <span>max lat</span>
          <input type="number" step="any" value={maxLat} onChange={(e) => setMaxLat(e.target.value)} />
        </label>
      </fieldset>

      <div className="dates">
        <label className="field">
          <span>start</span>
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="field">
          <span>end</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <button type="submit" className="submit" disabled={disabled}>
          {disabled ? 'Searching…' : 'Search'}
        </button>
      </div>

      {error && <p className="form-error">{error}</p>}
    </form>
  );
}
