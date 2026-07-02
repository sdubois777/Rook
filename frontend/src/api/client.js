import axios from 'axios'

// The API is served under /api (so the root namespace is free for the SPA).
// VITE_API_URL is the base DOMAIN (e.g. https://rookff.com) — append /api here
// so the Railway env var stays a plain domain. In dev (no VITE_API_URL) the
// Vite proxy forwards /api to the backend.
export const API_BASE = import.meta.env.VITE_API_URL
  ? `${import.meta.env.VITE_API_URL}/api`
  : '/api'

const api = axios.create({
  baseURL: API_BASE,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

// Token getter — registered by AuthProvider once Clerk is loaded
let _getToken = null

export function registerTokenGetter(fn) {
  _getToken = fn
}

/**
 * Get a Clerk token, retrying with exponential backoff. On a hard refresh the
 * first requests can fire before AuthProvider has registered _getToken (or
 * before Clerk has a token), which would 401. Retry until a token appears or we
 * give up. `getter` is injectable for tests; defaults to the registered getter.
 */
export async function getTokenWithRetry(retries = 5, delayMs = 100, getter = null) {
  let delay = delayMs
  for (let i = 0; i < retries; i++) {
    const fn = getter || _getToken
    if (fn) {
      try {
        const token = await fn()
        if (token) return token
      } catch {
        // not ready yet — fall through to backoff
      }
    }
    await new Promise((r) => setTimeout(r, delay))
    delay *= 2
  }
  return null
}

// Request interceptor — attach the auth token (waiting for Clerk if needed)
api.interceptors.request.use(
  async (config) => {
    const token = await getTokenWithRetry()
    if (token) config.headers.Authorization = `Bearer ${token}`
    return config
  },
  (error) => Promise.reject(error),
)

// Response interceptor — retry a 401 ONCE with a fresh token (covers the
// refresh race where the first request went out tokenless), then fall back to
// the existing auth-error signal.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const status = error.response?.status
    const config = error.config
    if (status === 401 && config && !config._retried) {
      config._retried = true
      const token = await getTokenWithRetry(3, 200)
      if (token) {
        config.headers = config.headers || {}
        config.headers.Authorization = `Bearer ${token}`
        return api(config)
      }
    }
    if (status === 401) {
      window.dispatchEvent(new Event('clerk:auth-error'))
    }
    // Billing gates (backend is the security boundary; these events only drive
    // UX affordances). feature_not_available (wrong tier) -> upgrade prompt;
    // insufficient_credits (402) -> out-of-credits prompt. Detail carries the
    // backend body (required_tier / required / available).
    const code = error.response?.data?.error
    if (status === 403 && code === 'feature_not_available') {
      window.dispatchEvent(
        new CustomEvent('billing:feature-required', { detail: error.response.data })
      )
    }
    if (status === 402 && code === 'insufficient_credits') {
      window.dispatchEvent(
        new CustomEvent('billing:insufficient-credits', { detail: error.response.data })
      )
    }
    // A parked (over-cap) league was used for a tier-gated action.
    if (status === 403 && code === 'league_suspended') {
      window.dispatchEvent(
        new CustomEvent('billing:league-suspended', { detail: error.response.data })
      )
    }
    return Promise.reject(error)
  },
)

export { api as apiClient }
export default api
