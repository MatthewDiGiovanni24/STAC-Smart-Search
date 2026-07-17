import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// In dev, proxy API paths to the backend so the browser talks to :5173 only
// (no CORS). Override the target with VITE_API_URL.
const target = process.env.VITE_API_URL || 'http://localhost:8000';
const proxy = Object.fromEntries(['/search', '/health', '/ready', '/catalogs'].map((path) => [
    path,
    { target, changeOrigin: true },
]));
export default defineConfig({
    plugins: [react()],
    server: { port: 5173, proxy },
});
