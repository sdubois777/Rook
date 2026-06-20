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

  it('LeagueSetup reuses the shared API_BASE (single source of truth)', () => {
    let src
    try {
      src = readFileSync('src/pages/LeagueSetup.jsx', 'utf-8')
    } catch {
      src = readFileSync('frontend/src/pages/LeagueSetup.jsx', 'utf-8')
    }
    expect(src).toMatch(/import\s*\{[^}]*API_BASE[^}]*\}\s*from\s*'\.\.\/api\/client'/)
    expect(src).not.toMatch(/import\.meta\.env\.VITE_API_URL/)
  })
})
