import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import NewsFeedItem from '../components/shared/NewsFeedItem'

const mockSignal = {
  id: '1',
  signal_type: 'injury_update',
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
    expect(screen.getByText('injury update')).toBeInTheDocument()
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
