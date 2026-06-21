/**
 * Page-refresh recovery for the live draft room.
 *
 * Repro: refresh mid-draft → the backend session is intact (#96) but the
 * in-memory Zustand store resets to empty. rehydrate() must pull the full view
 * back from the server: rosters, budgets, opponents, the drafted-filtered
 * available list, AND a real (re-fetched) suggested pick — not a placeholder.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useDraftStore } from '../stores/draft'

vi.mock('../api/draft', () => ({
  startDraft: vi.fn(),
  endDraft: vi.fn(),
  getDraftState: vi.fn(),
  getOpponentBudgets: vi.fn(),
  getRecommendation: vi.fn(),
  getAvailablePlayers: vi.fn(),
}))

import {
  getDraftState,
  getOpponentBudgets,
  getRecommendation,
  getAvailablePlayers,
} from '../api/draft'

// A realistic mid-draft snapshot: you have Bijan; opponent 'opp_a' has Chase.
function mockActiveDraft() {
  getDraftState.mockResolvedValue({
    your_remaining_budget: 146,
    spendable_on_next_player: 132,
    roster_slots_remaining: 14,
    your_roster: [
      { player_id: 'nfl_1', player_name: 'Bijan Robinson', position: 'RB', price: 54 },
    ],
    positional_counts: { RB: 1 },
  })
  getOpponentBudgets.mockResolvedValue({
    opponents: {
      opp_a: {
        budget: 139,
        roster_count: 1,
        threat_score: 61,
        combos: [],
        roster: [{ player_name: "Ja'Marr Chase", position: 'WR', price: 61 }],
      },
    },
  })
  // The current suggested pick — a REAL recommendation, computed server-side.
  getRecommendation.mockResolvedValue({
    type: 'recommendation',
    action: 'draft',
    player_name: 'Sam LaPorta',
    position: 'TE',
    reasoning: 'Best value on the board.',
  })
  // Full board pool: includes the two drafted players + two still-available.
  getAvailablePlayers.mockResolvedValue({
    tiers: {
      1: [
        { id: 'b1', name: 'Bijan Robinson', position: 'RB' },        // mine → filtered
        { id: 'c1', name: "Ja'Marr Chase", position: 'WR' },          // opp → filtered
        { id: 's1', name: 'Sam LaPorta', position: 'TE' },            // available
        { id: 'j1', name: 'Jonathan Taylor', position: 'RB' },        // available
      ],
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  // Simulate a fresh page load: store reset to its empty mount state.
  useDraftStore.setState({
    phase: 'setup',
    myRoster: [],
    myBudget: 200,
    rosterSlotsRemaining: 16,
    availablePlayers: [],
    teamPicks: {},
    teamThreatScores: {},
    opponentBudgets: {},
    recommendation: null,
    currentNomination: null,
  })
  try {
    localStorage.setItem('rook_draft_team', 'My Team')
  } catch { /* ignore */ }
})

describe('draft rehydrate on page refresh', () => {
  it('rehydrates rosters, budgets, opponents, filtered available, AND the suggested pick', async () => {
    mockActiveDraft()

    const ok = await useDraftStore.getState().rehydrate()
    expect(ok).toBe(true)

    const s = useDraftStore.getState()

    // Entered the live room.
    expect(s.phase).toBe('live')

    // Your roster + budget restored.
    expect(s.myRoster.map((p) => p.player_name)).toEqual(['Bijan Robinson'])
    expect(s.myBudget).toBe(146)
    expect(s.rosterSlotsRemaining).toBe(14)

    // Opponents restored (team rosters + budgets + threat).
    expect(s.teamPicks.opp_a.map((p) => p.player_name)).toEqual(["Ja'Marr Chase"])
    expect(s.opponentBudgets.opp_a).toBe(139)
    expect(s.teamThreatScores.opp_a).toBe(61)
    // Your own roster grouped under your team name (from localStorage).
    expect(s.teamPicks['My Team'].map((p) => p.player_name)).toEqual(['Bijan Robinson'])

    // Available = pool MINUS everyone drafted (Bijan + Chase gone; others stay).
    const names = s.availablePlayers.map((p) => p.name)
    expect(names).toContain('Sam LaPorta')
    expect(names).toContain('Jonathan Taylor')
    expect(names).not.toContain('Bijan Robinson')
    expect(names).not.toContain("Ja'Marr Chase")

    // The suggested pick is a REAL re-fetched recommendation — not blank/placeholder.
    expect(s.recommendation).not.toBeNull()
    expect(s.recommendation.player_name).toBe('Sam LaPorta')
    expect(s.recommendation.action).toBe('draft')
    // The nominee card is recovered from the rec (live bid/clock arrive next tick).
    expect(s.currentNomination?.playerName).toBe('Sam LaPorta')
  })

  it('does NOT force the draft room when there is no active draft (409)', async () => {
    getDraftState.mockRejectedValue({ response: { status: 409 } })

    const ok = await useDraftStore.getState().rehydrate()

    expect(ok).toBe(false)
    const s = useDraftStore.getState()
    expect(s.phase).toBe('setup') // stays in setup — no phantom room
    expect(s.myRoster).toEqual([])
    // The other endpoints must not have been called once /state said "no draft".
    expect(getOpponentBudgets).not.toHaveBeenCalled()
    expect(getRecommendation).not.toHaveBeenCalled()
  })

  it('still hydrates rosters/budget when opponents or recommendation fetch fails', async () => {
    mockActiveDraft()
    getOpponentBudgets.mockRejectedValue(new Error('network'))
    getRecommendation.mockRejectedValue(new Error('network'))

    const ok = await useDraftStore.getState().rehydrate()
    expect(ok).toBe(true)

    const s = useDraftStore.getState()
    expect(s.phase).toBe('live')
    expect(s.myRoster.map((p) => p.player_name)).toEqual(['Bijan Robinson'])
    expect(s.myBudget).toBe(146)
    // Available still filters out your own drafted player even with no opponents.
    expect(s.availablePlayers.map((p) => p.name)).not.toContain('Bijan Robinson')
  })
})
