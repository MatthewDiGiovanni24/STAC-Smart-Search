import { useCallback, useState } from 'react';
import { SearchForm } from './components/SearchForm';
import { ResultsStream } from './components/ResultsStream';
import type { SearchPayload } from './types';
import ThemeToggle from './components/ThemeToggle';

export default function App() {
  // A fresh object per submit re-triggers ResultsStream's effect.
  const [query, setQuery] = useState<SearchPayload | null>(null);
  const [searching, setSearching] = useState(false);

  const onSearch = useCallback((payload: SearchPayload) => {
    setSearching(true);
    setQuery({ ...payload });
  }, []);

  const onSettled = useCallback(() => setSearching(false), []);

  return (
    <div className="app">
    <ThemeToggle />
      <header className="masthead">
        <h1>STAC Smart Search</h1>
        <p className="tagline">
          Semantic search across federated STAC catalogs — one query, fanned out,
          ranked by relevance, streamed as results arrive.
        </p>
      </header>

      <SearchForm onSearch={onSearch} disabled={searching} />

      <ResultsStream query={query} onSettled={onSettled} />

      <footer className="foot">
        Results stream via Server-Sent Events; cards re-sort when the final
        ranking arrives.
      </footer>
    </div>
  );
}
