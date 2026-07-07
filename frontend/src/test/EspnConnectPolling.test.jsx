import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

// Timer-driven async state updates must flush inside act() to reach the DOM.
const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

// The ESPN Connect wizard step polls /account/leagues (via fetchUserLeagues) to
// learn that the extension connected the league out-of-band, then advances to the
// shared "League Connected!" done screen — the same success mechanism the
// Sleeper/manual-ESPN paths use.
vi.mock('../api/league', () => ({
  fetchUserLeagues: vi.fn(),
  fetchYahooConnectUrl: vi.fn(),
}))
vi.mock('../api/client', () => ({
  apiClient: { post: vi.fn(), get: vi.fn() },
  API_BASE: 'http://test/api',
}))

import LeagueSetup from '../pages/LeagueSetup'
import { fetchUserLeagues } from '../api/league'
import { apiClient } from '../api/client'

const ESPN_LEAGUE = {
  id: 'uuid-1',
  platform: 'espn',
  league_id: 'L1',
  league_name: 'My ESPN League',
  last_synced: '2026-07-07T00:00:00Z',
}

function renderWizardOnEspnStep() {
  render(
    <MemoryRouter>
      <LeagueSetup />
    </MemoryRouter>
  )
  // Platform step → choose ESPN → Connect step (EspnConnect mounts + polls).
  fireEvent.click(screen.getByRole('button', { name: /ESPN/i }))
}

beforeEach(() => {
  vi.useFakeTimers()
  fetchUserLeagues.mockReset()
  apiClient.post.mockReset()
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
})

describe('ESPN Connect step — extension-connect detection', () => {
  it('advances to the done screen when a NEW ESPN league appears', async () => {
    // baseline (none) → immediate poll (none) → interval poll finds the league.
    fetchUserLeagues
      .mockResolvedValueOnce([]) // baseline on mount
      .mockResolvedValueOnce([]) // immediate poll
      .mockResolvedValue([ESPN_LEAGUE]) // subsequent polls

    renderWizardOnEspnStep()
    await advance(0) // flush baseline + immediate poll
    expect(screen.getByText(/Waiting for the extension/i)).toBeInTheDocument()

    await advance(3000) // first interval poll → detect → advance
    expect(screen.getByText(/League Connected!/i)).toBeInTheDocument()
    expect(screen.getByText('My ESPN League')).toBeInTheDocument()

    // Poller stopped after advancing — no further fetches on later ticks.
    const callsAfterAdvance = fetchUserLeagues.mock.calls.length
    await advance(9000)
    expect(fetchUserLeagues.mock.calls.length).toBe(callsAfterAdvance)
  })

  it('does NOT advance for a user who already had an ESPN league (baseline)', async () => {
    fetchUserLeagues.mockResolvedValue([ESPN_LEAGUE]) // present from the start

    renderWizardOnEspnStep()
    await advance(0)
    await advance(3000)

    expect(screen.queryByText(/League Connected!/i)).not.toBeInTheDocument()
    expect(screen.getByText(/Waiting for the extension/i)).toBeInTheDocument()
  })

  it('advances when an existing ESPN league is re-synced (last_synced moves)', async () => {
    fetchUserLeagues
      .mockResolvedValueOnce([ESPN_LEAGUE]) // baseline
      .mockResolvedValueOnce([ESPN_LEAGUE]) // immediate poll
      .mockResolvedValue([{ ...ESPN_LEAGUE, last_synced: '2026-07-08T00:00:00Z' }])

    renderWizardOnEspnStep()
    await advance(0)
    await advance(3000)

    expect(screen.getByText(/League Connected!/i)).toBeInTheDocument()
  })

  it('shows the timeout + manual fallback after the polling window', async () => {
    fetchUserLeagues.mockResolvedValue([]) // never connects

    renderWizardOnEspnStep()
    await advance(0)
    expect(screen.getByText(/Waiting for the extension/i)).toBeInTheDocument()

    await advance(90000)
    expect(screen.getByText(/Still waiting/i)).toBeInTheDocument()
    expect(screen.queryByText(/Waiting for the extension/i)).not.toBeInTheDocument()
    // Manual fallback remains available.
    expect(
      screen.getByRole('button', { name: /Enter cookies manually/i })
    ).toBeInTheDocument()
  })

  it('manual entry still advances independently of polling', async () => {
    fetchUserLeagues.mockResolvedValue([]) // polling finds nothing
    apiClient.post.mockResolvedValue({
      data: { platform: 'espn', picks_imported: 5, seasons_imported: 1 },
    })

    renderWizardOnEspnStep()
    await advance(0)

    fireEvent.click(screen.getByRole('button', { name: /Enter cookies manually/i }))
    fireEvent.change(screen.getByPlaceholderText('12345678'), { target: { value: 'L9' } })
    fireEvent.change(screen.getByPlaceholderText('Paste espn_s2 value'), {
      target: { value: 's2' },
    })
    fireEvent.change(
      screen.getByPlaceholderText('{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}'),
      { target: { value: '{swid}' } }
    )
    fireEvent.click(screen.getByRole('button', { name: /Connect ESPN League/i }))
    await advance(0)

    expect(apiClient.post).toHaveBeenCalledWith('/leagues/connect/espn', {
      league_id: 'L9',
      espn_s2: 's2',
      swid: '{swid}',
    })
    expect(screen.getByText(/League Connected!/i)).toBeInTheDocument()
  })
})
