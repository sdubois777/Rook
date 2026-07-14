import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import NewsFeedItem from '../components/shared/NewsFeedItem'

// signal_type MUST be one of the real agent types (SIGNAL_TYPES in
// backend/agents/beat_reporter.py) so the icon/color mapping is exercised the
// way production data drives it.
const mockSignal = {
  id: '1',
  signal_type: 'injury_flag',
  source: 'https://www.espn.com/espn/rss/nfl/news',
  raw_text: 'Player X suffered a knee injury in practice.',
  confidence: 'high',
  flagged_at: '2026-05-10T14:00:00Z',
  player_name: 'Player X',
  player_team: 'LAC',
  player_id: 'abc-123',
}

describe('NewsFeedItem', () => {
  it('renders signal type label', () => {
    render(<NewsFeedItem signal={mockSignal} />)
    expect(screen.getByText('injury flag')).toBeInTheDocument()
  })

  it('colors the label by the real agent signal type (not the gray fallback)', () => {
    render(<NewsFeedItem signal={mockSignal} />)
    // injury_flag → red; a mismatched key would fall back to slate-400.
    expect(screen.getByText('injury flag')).toHaveClass('text-red-400')
  })

  it('renders player name', () => {
    render(<NewsFeedItem signal={mockSignal} />)
    expect(screen.getByText('Player X')).toBeInTheDocument()
  })

  it('shows the headline excerpt without needing a click', () => {
    render(<NewsFeedItem signal={mockSignal} />)
    expect(screen.getByText(/knee injury/)).toBeInTheDocument()
  })

  it('shows the source publisher derived from the feed URL', () => {
    render(<NewsFeedItem signal={mockSignal} />)
    expect(screen.getByText(/via ESPN/)).toBeInTheDocument()
  })

  it('calls onPlayerClick when player name is clicked', () => {
    const onPlayerClick = vi.fn()
    render(<NewsFeedItem signal={mockSignal} onPlayerClick={onPlayerClick} />)
    fireEvent.click(screen.getByText('Player X'))
    expect(onPlayerClick).toHaveBeenCalledWith('abc-123')
  })
})
