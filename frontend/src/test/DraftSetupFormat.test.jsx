/**
 * DraftSetup sends the RESOLVED draft type to POST /draft/start.
 *
 * This is not a cosmetic default. `startDraft({ draftType })` becomes `body.draft_type`
 * (src/api/draft.js), which selects the backend's snake vs auction recommendation engine.
 * DraftSetup used to carry its own `selectedLeague?.draft_type || 'auction'` fallback, so a
 * user with no synced league saw the snake UI on every surface and silently started the
 * AUCTION engine. Nothing caught it: the divergence is invisible until a live draft runs.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { readFileSync } from 'fs'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ started: [] }))

vi.mock('../hooks/useEntitlements', () => ({
  useEntitlements: () => ({ tierLimits: { live_draft: true } }),
  isFeatureLocked: () => false,
}))

vi.mock('../stores/draft', () => ({
  useDraftStore: (selector) => selector({ startDraft: async (opts) => h.started.push(opts) }),
}))

import DraftSetup from '../components/draft/DraftSetup'
import { LeagueContext } from '../context/LeagueContext'

function ctx(over = {}) {
  return {
    selectedLeague: null,
    setSelectedLeague: () => {},
    draftType: 'snake',
    isSnake: true,
    isAuction: false,
    scoringFormat: 'ppr',
    formatOverride: null,
    setFormatOverride: () => {},
    canChooseFormat: true,
    ...over,
  }
}

function renderSetup(value) {
  return render(
    <MemoryRouter>
      <LeagueContext.Provider value={value}>
        <DraftSetup />
      </LeagueContext.Provider>
    </MemoryRouter>
  )
}

describe('DraftSetup — draft_type is the engine contract', () => {
  beforeEach(() => {
    h.started = []
  })

  it('sends snake for a no-league user (the new default), NOT auction', async () => {
    renderSetup(ctx())
    fireEvent.click(screen.getByRole('button', { name: /Start Draft/i }))
    await waitFor(() => expect(h.started).toHaveLength(1))
    expect(h.started).toHaveLength(1)
    expect(h.started[0].draftType).toBe('snake')
  })

  it('sends auction when the resolved context says auction', async () => {
    renderSetup(ctx({ draftType: 'auction', isSnake: false, isAuction: true }))
    fireEvent.click(screen.getByRole('button', { name: /Start Draft/i }))
    await waitFor(() => expect(h.started).toHaveLength(1))
    expect(h.started[0].draftType).toBe('auction')
  })

  it('passes the synced league id through alongside it', async () => {
    const league = { id: 'lg1', draft_type: 'auction', scoring: 'ppr' }
    renderSetup(ctx({
      selectedLeague: league, draftType: 'auction',
      isSnake: false, isAuction: true, canChooseFormat: false,
    }))
    fireEvent.click(screen.getByRole('button', { name: /Start Draft/i }))
    await waitFor(() => expect(h.started).toHaveLength(1))
    expect(h.started[0]).toMatchObject({ leagueId: 'lg1', draftType: 'auction' })
  })

  it('never re-derives its own default — source carries no `|| \'auction\'`', () => {
    // A source-level guard, in the spirit of the playerUtils raw-field guard: the runtime
    // tests above only cover the contexts they construct, whereas a reintroduced private
    // fallback would silently win for whatever case they miss.
    // cwd-relative with a repo-root fallback — the house pattern (see
    // playerUtils.test.jsx and DraftBoardLeague.test.jsx), since vitest may run from
    // either frontend/ or the repo root.
    const rel = 'src/components/draft/DraftSetup.jsx'
    let src
    try {
      src = readFileSync(rel, 'utf-8')
    } catch {
      src = readFileSync(`frontend/${rel}`, 'utf-8')
    }
    // Strip comments first — the file DOCUMENTS the removed fallback, and a guard that
    // trips on its own explanation would push the next author to delete the explanation.
    const code = src
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '')
    expect(code).not.toMatch(/\|\|\s*['"]auction['"]/)
    expect(code).not.toMatch(/\|\|\s*['"]snake['"]/)
    // …and prove the strip didn't just blank the file out.
    expect(code).toMatch(/startDraft\(/)
    expect(code).toMatch(/draftType/)
  })
})
