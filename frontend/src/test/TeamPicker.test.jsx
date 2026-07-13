import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/leagues', () => ({ setMyTeam: vi.fn(async () => ({ my_team_id: '5', my_team_id_source: 'manual' })) }))

import { setMyTeam } from '../api/leagues'
import TeamPicker from '../components/TeamPicker'

const TEAMS = [{ team_id: '2', name: 'Team Alpha' }, { team_id: '5', name: 'Bob' }]

describe('TeamPicker (bind-failure recovery)', () => {
  beforeEach(() => setMyTeam.mockClear())

  it('renders every team (blank name falls back to a usable label)', () => {
    render(<TeamPicker leagueId="lg-1" teams={[...TEAMS, { team_id: '7', name: '' }]} onPicked={() => {}} />)
    expect(screen.getByText('Team Alpha')).toBeInTheDocument()
    expect(screen.getByText('Bob')).toBeInTheDocument()
    expect(screen.getByText('Team 7')).toBeInTheDocument() // blank name → "Team <id>"
  })

  it('picking a team PATCHes my_team_id and calls onPicked', async () => {
    const onPicked = vi.fn()
    render(<TeamPicker leagueId="lg-1" teams={TEAMS} onPicked={onPicked} />)
    fireEvent.click(screen.getByText('Bob'))
    await waitFor(() => expect(setMyTeam).toHaveBeenCalledWith('lg-1', '5'))
    await waitFor(() => expect(onPicked).toHaveBeenCalled())
  })
})
