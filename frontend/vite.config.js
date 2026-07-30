/* global process -- this config is evaluated by Vite in Node, not in the browser. */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev API target. Defaults to plain HTTP so nothing changes for normal work.
//
// Set VITE_API_TARGET=https://localhost:8000 when running the backend over TLS
// (scripts/make_dev_cert.py), which is required to exercise the Yahoo OAuth callback
// locally — Yahoo refuses to register an http:// redirect URI, so YAHOO_REDIRECT_URI must
// be https and the dev server has to actually speak it.
const API_TARGET = process.env.VITE_API_TARGET || 'http://localhost:8000'
const WS_TARGET = API_TARGET.replace(/^http/, 'ws')

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
      // `secure: false` accepts the self-signed dev cert; it is inert over plain HTTP
      // and this config is never used in production (prod serves the SPA from the
      // backend, no proxy involved).
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        secure: false,
      },
      '/ws': {
        target: WS_TARGET,
        ws: true,
        secure: false,
      },
      // /terms and /privacy are SERVER-RENDERED by the backend (before the SPA
      // catch-all). The Vite dev server would otherwise client-route them into the
      // SPA → /dashboard, so forward them (and their .html aliases) to the backend.
      // Dev-only — prod serves these directly from the backend.
      '/terms': { target: API_TARGET, changeOrigin: true, secure: false },
      '/privacy': { target: API_TARGET, changeOrigin: true, secure: false },
    },
  },
})
