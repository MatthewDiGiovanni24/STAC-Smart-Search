import { useEffect, useState } from 'react';

export default function ThemeToggle() {
  const [theme, setTheme] = useState(() => {
    const saved = localStorage.getItem('theme');
    if (saved) return saved;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  return (
    <button
      type="button"
      onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
      title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
      style={{
        position: 'absolute',
        top: '1rem',
        right: '1rem',
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        color: 'var(--text)',
        fontSize: '1.2rem',
        cursor: 'pointer',
        padding: '0.5rem',
        borderRadius: '8px',
        zIndex: 1000,
        boxShadow: 'var(--shadow)'
      }}
    >
      {theme === 'light' ? '⏾' : '🔆'}
    </button>
  );
}