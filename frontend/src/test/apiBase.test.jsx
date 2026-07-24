import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { API_BASE } from '../api/client'

describe('API served under /api', () => {
  it('client API_BASE ends with /api', () => {
    // Default (no VITE_API_URL) -> "/api"; with a domain -> "<domain>/api".
    expect(API_BASE.endsWith('/api')).toBe(true)
  })

  it('client.js appends /api to VITE_API_URL (base domain stays plain)', () => {
    let src
    try {
      src = readFileSync('src/api/client.js', 'utf-8')
    } catch {
      src = readFileSync('frontend/src/api/client.js', 'utf-8')
    }
    expect(src).toMatch(/\$\{import\.meta\.env\.VITE_API_URL\}\/api/)
  })

  it('draft WebSocket connects under /api', () => {
    let src
    try {
      src = readFileSync('src/hooks/useDraftSocket.js', 'utf-8')
    } catch {
      src = readFileSync('frontend/src/hooks/useDraftSocket.js', 'utf-8')
    }
    expect(src).toMatch(/const WS_PATH = '\/api\/draft\/ws\/draft'/)
  })

  it('LeagueSetup never hardcodes the API base (delegates to the api client)', () => {
    // The Yahoo OAuth initiation now goes through fetchYahooConnectUrl() on the
    // shared axios client (which owns baseURL), so LeagueSetup builds no raw API
    // URLs itself. The invariant that still matters: it must not reach past the
    // client to read the API base from the environment directly.
    let src
    try {
      src = readFileSync('src/pages/LeagueSetup.jsx', 'utf-8')
    } catch {
      src = readFileSync('frontend/src/pages/LeagueSetup.jsx', 'utf-8')
    }
    expect(src).not.toMatch(/import\.meta\.env\.VITE_API_URL/)
  })
})
