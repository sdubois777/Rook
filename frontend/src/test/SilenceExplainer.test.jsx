import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import SilenceExplainer from '../components/trade/SilenceExplainer'

const NEAR_MISS = {
  give: [{ id: 'a', name: 'Dak Prescott', position: 'QB' }, { id: 'b', name: 'Evan Engram', position: 'TE' }],
  get: [{ id: 'c', name: 'George Pickens', position: 'WR' }],
  would_be_ppg: 3.2,
  shortfall_reason: 'it improves your lineup, but not by the full margin',
}

describe('SilenceExplainer — explain the silence', () => {
  it('renders the reason message when a context is present', () => {
    render(<SilenceExplainer context={{ reason: 'lineup_too_strong', message: 'Your starting lineup is strong enough that no fair trade improves it meaningfully right now.', near_miss: null }} />)
    expect(screen.getByText(/strong enough that no fair trade/)).toBeInTheDocument()
    // no near-miss card when near_miss is null
    expect(screen.queryByText(/Closest possible deal/)).not.toBeInTheDocument()
  })

  it('renders the near-miss DISTINCTLY, labeled as a negotiation starter (not a rec)', () => {
    render(<SilenceExplainer context={{ reason: 'scarcity', message: 'Locked into other lineups.', near_miss: NEAR_MISS }} />)
    expect(screen.getByText(/Closest possible deal/)).toBeInTheDocument()
    expect(screen.getByText(/would take some convincing/)).toBeInTheDocument()
    expect(screen.getByText(/Not a recommendation/)).toBeInTheDocument()
    // carries give/get, the would-be ppg, and the shortfall reason
    expect(screen.getByText(/Dak Prescott \+ Evan Engram/)).toBeInTheDocument()
    expect(screen.getByText(/George Pickens/)).toBeInTheDocument()
    expect(screen.getByText(/\+3\.2/)).toBeInTheDocument()
    expect(screen.getByText(/not by the full margin/)).toBeInTheDocument()
  })

  it('falls back to the plain message when there is no context', () => {
    render(<SilenceExplainer context={null} fallbackMessage="no clear trade right now" />)
    expect(screen.getByText(/no clear trade right now/)).toBeInTheDocument()
    expect(screen.queryByText(/Closest possible deal/)).not.toBeInTheDocument()
  })

  it('renders nothing when there is neither context nor a fallback (trades exist)', () => {
    const { container } = render(<SilenceExplainer context={null} fallbackMessage={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })
})
