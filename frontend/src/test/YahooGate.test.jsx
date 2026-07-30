import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

// Yahoo moved the Fantasy Sports API behind an approval process and every endpoint
// 403s for this app, so the connect flow cannot succeed. constants.YAHOO_ENABLED gates
// it. These tests pin the two paths a tile-only gate silently misses — both reach
// window.location.href with NO user click, via a 2s timer.
//
// When Yahoo access lands and YAHOO_ENABLED flips to true, this whole file should be
// deleted (or inverted) — it asserts the gated behaviour, not a permanent invariant.

const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

vi.mock('../api/league', () => ({
  fetchUserLeagues: vi.fn(() => Promise.resolve([])),
  fetchYahooConnectUrl: vi.fn(),
}))
vi.mock('../api/client', () => ({
  apiClient: { post: vi.fn(), get: vi.fn() },
  API_BASE: 'http://test/api',
}))

import LeagueSetup from '../pages/LeagueSetup'
import { fetchYahooConnectUrl } from '../api/league'
import { YAHOO_ENABLED } from '../lib/constants'

function renderAt(path) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <LeagueSetup />
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.useFakeTimers()
  fetchYahooConnectUrl.mockReset()
  fetchYahooConnectUrl.mockResolvedValue('https://api.login.yahoo.com/oauth2/request_auth')
})
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Yahoo gate', () => {
  it.runIf(!YAHOO_ENABLED)('disables the Yahoo tile and explains why', () => {
    renderAt('/league-setup')
    expect(screen.getByRole('button', { name: /Yahoo/i })).toBeDisabled()
    expect(screen.getByText(/Yahoo import is paused/i)).toBeInTheDocument()
    // ESPN and Sleeper must stay live — the gate is Yahoo-only.
    expect(screen.getByRole('button', { name: /ESPN/i })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: /Sleeper/i })).not.toBeDisabled()
  })

  // The retry branch calls fetchYahooConnectUrl() + window.location.href from inside a
  // setTimeout with no user interaction. A stale bookmark or history entry pointing
  // here would auto-launch the dead OAuth flow. A guard placed AFTER that branch is
  // too late, which is why this asserts the network call never happens at all.
  it.runIf(!YAHOO_ENABLED)('does not auto-relaunch OAuth from the account_not_ready retry link', async () => {
    renderAt('/league-setup?platform=yahoo&error=account_not_ready&retry=true')
    await advance(5000)
    expect(fetchYahooConnectUrl).not.toHaveBeenCalled()
    expect(screen.getByText(/Choose Your Platform/i)).toBeInTheDocument()
  })

  // The trap: LeagueSetup reads `setPlatform(p || 'yahoo')` in both OAuth-error
  // branches, so a URL with an error and NO platform param still resolves to Yahoo.
  // A `p === 'yahoo'` guard alone leaves this fully live.
  it.runIf(!YAHOO_ENABLED)('short-circuits an error deep link that carries no platform param', async () => {
    renderAt('/league-setup?error=invalid_state')
    await advance(5000)
    expect(fetchYahooConnectUrl).not.toHaveBeenCalled()
    expect(screen.getByText(/Choose Your Platform/i)).toBeInTheDocument()
    expect(screen.queryByText(/Connect Yahoo Fantasy/i)).not.toBeInTheDocument()
  })

  it.runIf(!YAHOO_ENABLED)('never renders the Yahoo connect step, however the user arrives', async () => {
    renderAt('/league-setup?platform=yahoo')
    await advance(100)
    expect(screen.queryByRole('button', { name: /Connect with Yahoo/i })).not.toBeInTheDocument()
  })

  it.runIf(!YAHOO_ENABLED)('refuses to start OAuth from the api layer', async () => {
    const { fetchYahooConnectUrl: real } = await vi.importActual('../api/league')
    await expect(real()).rejects.toThrow(/disabled/i)
  })

  it('leaves ESPN reachable from the platform picker', () => {
    renderAt('/league-setup')
    fireEvent.click(screen.getByRole('button', { name: /ESPN/i }))
    expect(screen.getByText(/Connect ESPN via the Rook extension/i)).toBeInTheDocument()
  })
})
