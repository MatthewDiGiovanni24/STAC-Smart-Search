import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In dev, proxy API paths to the backend so the browser talks to :5173 only
// (no CORS). Override the target with VITE_API_URL.
// Use 127.0.0.1 (not "localhost") so the dev proxy doesn't resolve to IPv6 ::1
// when the backend is bound to IPv4 — a common macOS mismatch.
const target = process.env.VITE_API_URL || 'http://127.0.0.1:8000';
const proxy = Object.fromEntries(
  ['/search', '/health', '/ready', '/catalogs'].map((path) => [
    path,
    { target, changeOrigin: true },
  ]),
);

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
});
