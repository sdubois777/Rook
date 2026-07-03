import { describe, it, expect, beforeEach } from 'vitest'
import { useDraftStore } from '../stores/draft'

describe('draft store upgradeTeamName (mid-draft label upgrade)', () => {
  beforeEach(() => {
    useDraftStore.setState({ myTeamName: 'Your Team' })
    try { localStorage.removeItem('rook_draft_team') } catch { /* ignore */ }
  })

  it('upgrades the generic label to a real derived name', () => {
    useDraftStore.getState().upgradeTeamName('Gridiron Gang')
    expect(useDraftStore.getState().myTeamName).toBe('Gridiron Gang')
    expect(localStorage.getItem('rook_draft_team')).toBe('Gridiron Gang')
  })

  it('is non-destructive once a real name is set', () => {
    useDraftStore.setState({ myTeamName: 'Real Name' })
    useDraftStore.getState().upgradeTeamName('Later Name')
    expect(useDraftStore.getState().myTeamName).toBe('Real Name')
  })

  it('a blank/undefined name is a no-op (never blanks the label)', () => {
    useDraftStore.getState().upgradeTeamName('')
    useDraftStore.getState().upgradeTeamName(undefined)
    useDraftStore.getState().upgradeTeamName('   ')
    expect(useDraftStore.getState().myTeamName).toBe('Your Team')
  })
})
