import { render, screen } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
import { LeagueContext } from '../context/LeagueContext'
import TeamRosterPanel from '../components/draft/TeamRosterPanel'
import { useDraftStore } from '../stores/draft'

function renderPanel(isSnake) {
  return render(
    <LeagueContext.Provider value={{ isSnake }}>
      <TeamRosterPanel />
    </LeagueContext.Provider>
  )
}

describe('TeamRosterPanel snake vs auction display', () => {
  beforeEach(() => {
    useDraftStore.setState({
      myTeamName: 'Your Team',
      selectedTeam: null,
      teamsState: {},
      teamThreatScores: {},
      teamPicks: {},
      myRoster: [
        { player_id: 'p1', player_name: 'Bijan Robinson', position: 'RB', price: 0 },
        { player_id: 'p2', player_name: 'Ja\'Marr Chase', position: 'WR', price: 0 },
      ],
    })
  })

  it('snake: hides the budget UI and per-pick prices; slots count from the roster', () => {
    renderPanel(true)
    // No salary-cap chrome on a snake draft.
    expect(screen.queryByText(/Budget:/)).not.toBeInTheDocument()
    expect(screen.queryByText('$0')).not.toBeInTheDocument()
    // Slots fall back to the displayed roster's pick count (no scraped slotsUsed).
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText(/\/\s*16 slots/)).toBeInTheDocument()
    expect(screen.getByText('Bijan Robinson')).toBeInTheDocument()
  })

  it('auction: budget UI and prices render; slots fall back to roster length too', () => {
    renderPanel(false)
    expect(screen.getByText(/Budget:/)).toBeInTheDocument()
    // ESPN salary never sends slotsUsed — the count must still update (was --/16).
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getAllByText('$0').length).toBeGreaterThan(0) // per-pick price shown
  })
})
