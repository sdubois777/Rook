import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.js'],
    globals: true,
  },
  server: {
    proxy: {
      // The backend now serves the API under /api, so forward /api/* as-is
      // (no rewrite). /ws is the app-level news WebSocket (stays at root).
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
      // /terms and /privacy are SERVER-RENDERED by the backend (before the SPA
      // catch-all). The Vite dev server would otherwise client-route them into the
      // SPA → /dashboard, so forward them (and their .html aliases) to the backend.
      // Dev-only — prod serves these directly from the backend.
      '/terms': { target: 'http://localhost:8000', changeOrigin: true },
      '/privacy': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
