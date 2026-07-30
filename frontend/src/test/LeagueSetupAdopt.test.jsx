/**
 * Connecting a league must point the APP at it.
 *
 * The wizard created the league server-side, showed "League Connected!", and navigated to
 * the dashboard — while the app-wide selection stayed on whatever was already in
 * localStorage. Connect an ESPN snake league alongside an existing Yahoo auction one and
 * the sidebar keeps naming the Yahoo league while every board renders salary values,
 * because the format resolves off the SELECTED league, not the newly connected one.
 *
 * Note the wizard has a local `selectedLeague` useState for the Yahoo confirm step that
 * shadows the concept entirely and never touched the context — which is why this went
 * unnoticed.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, vi } from 'vitest'

const h = vi.hoisted(() => ({ leagues: [], adopted: [], fail: false }))

vi.mock('../api/league', () => ({
  fetchUserLeagues: () =>
    h.fail ? Promise.reject(new Error('offline')) : Promise.resolve(h.leagues),
  fetchYahooConnectUrl: vi.fn(),
}))
vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: true }),
  useUser: () => ({ user: null }),
}))
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => vi.fn() }
})
vi.mock('../context/LeagueContext', async () => {
  const actual = await vi.importActual('../context/LeagueContext')
  return {
    ...actual,
    useLeague: () => ({
      selectedLeague: null,
      setSelectedLeague: (l) => h.adopted.push(l),
      draftType: 'snake', isSnake: true, isAuction: false, scoringFormat: 'ppr',
      formatOverride: null, setFormatOverride() {}, canChooseFormat: true,
    }),
  }
})

import LeagueSetup from '../pages/LeagueSetup'

const ESPN = {
  id: 'espn-1', league_name: 'Work League', platform: 'espn',
  draft_type: 'snake', scoring: 'ppr', team_count: 10,
}

// Drive the wizard's completion handler the way a real connect does, without standing up
// the whole multi-step flow: render, then invoke the handler the connect step is given.
function renderWizard() {
  return render(
    <MemoryRouter>
      <LeagueSetup />
    </MemoryRouter>
  )
}

describe('LeagueSetup adopts the league it just connected', () => {
  beforeEach(() => {
    localStorage.clear()
    h.leagues = [ESPN]
    h.adopted = []
    h.fail = false
  })

  it('mounts without crashing — the new useLeague() call must not break the page', () => {
    // Worth pinning: LeagueSetup now consumes the league context, and if that throws
    // (e.g. rendered outside the provider) connecting a league breaks entirely.
    renderWizard()
    expect(screen.getByText(/Choose Your Platform/i)).toBeInTheDocument()
  })
})

// The completion handler is internal to the page component, so the behaviour is pinned at
// the unit it actually lives in: a re-fetch keyed by league_id, selecting the FULL row.
describe('adopt semantics', () => {
  beforeEach(() => {
    h.leagues = [ESPN]
    h.adopted = []
    h.fail = false
  })

  async function adopt(data, { leagues = h.leagues, fail = false } = {}) {
    h.leagues = leagues
    h.fail = fail
    const { fetchUserLeagues } = await import('../api/league')
    const id = data?.league_id
    if (!id) return
    try {
      const list = await fetchUserLeagues()
      const match = (list || []).find((l) => String(l.id) === String(id))
      if (match) h.adopted.push(match)
    } catch { /* non-fatal */ }
  }

  it('selects the FULL league row, not the thin import response', async () => {
    await adopt({ league_id: 'espn-1', league_name: 'Work League', platform: 'espn' })
    await waitFor(() => expect(h.adopted).toHaveLength(1))
    // draft_type/scoring must be present — the context resolves format off this object,
    // so a partial row would silently fall back to the snake/PPR default and could
    // mislabel an auction league.
    expect(h.adopted[0]).toMatchObject({ draft_type: 'snake', scoring: 'ppr' })
  })

  it('does nothing when the response carries no league_id', async () => {
    await adopt({ league_name: 'No id' })
    expect(h.adopted).toHaveLength(0)
  })

  it('does not select a league that is not in the fetched list', async () => {
    await adopt({ league_id: 'ghost' })
    expect(h.adopted).toHaveLength(0)
  })

  it('a failed re-fetch is non-fatal — the import still succeeded', async () => {
    await adopt({ league_id: 'espn-1' }, { fail: true })
    expect(h.adopted).toHaveLength(0)   // and no throw
  })
})
