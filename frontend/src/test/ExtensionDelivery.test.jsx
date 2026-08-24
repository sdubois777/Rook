/**
 * Is the browser extension actually delivering draft updates, and does the room
 * say so? (#461)
 *
 * A customer reported "not synced to the draft on ESPN". Nothing in the product
 * could tell that state apart from a working draft: the connection dot reports
 * only the browser-to-Rook socket and stays green regardless, the extension
 * popup reads "Connected" whenever any text is saved in its token box, and the
 * extension discards the server's response status, so a refusal looks exactly
 * like success. These tests pin the two signals that close that gap.
 */
import { render, screen, act } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useDraftStore } from '../stores/draft'
import ExtensionStatus from '../components/draft/ExtensionStatus'
import ExtensionBlockedBanner from '../components/draft/ExtensionBlockedBanner'
import { formatElapsed } from '../utils/elapsed'

beforeEach(() => {
  useDraftStore.setState({ lastExtensionEventAt: null, extensionBlocked: null })
})

describe('formatElapsed', () => {
  it('is coarse and never rounds up', () => {
    expect(formatElapsed(0)).toBe('0s')
    expect(formatElapsed(3_000)).toBe('3s')
    expect(formatElapsed(59_999)).toBe('59s')
    expect(formatElapsed(60_000)).toBe('1m')
    expect(formatElapsed(119_000)).toBe('1m')     // not "2m"
    expect(formatElapsed(3_600_000)).toBe('1h')
  })

  it('floors a negative or unusable duration at zero', () => {
    // A clock tick that predates the newest event is normal, not an error.
    expect(formatElapsed(-5_000)).toBe('0s')
    expect(formatElapsed(NaN)).toBe('0s')
    expect(formatElapsed(undefined)).toBe('0s')
  })
})

describe('ExtensionStatus — has anything actually arrived?', () => {
  it('warns plainly when NOTHING has ever arrived — the reported bug', () => {
    render(<ExtensionStatus />)
    expect(screen.getByText('No draft data yet')).toBeInTheDocument()
  })

  it('tells the user what to check, rather than only that it is broken', () => {
    render(<ExtensionStatus />)
    const help = screen.getByTitle(/extension is installed/i)
    expect(help).toHaveAttribute('title', expect.stringContaining('draft page'))
    expect(help).toHaveAttribute('title', expect.stringContaining('draft token'))
  })

  it('reports elapsed time once updates are arriving', () => {
    vi.useFakeTimers()
    try {
      const t0 = Date.now()
      useDraftStore.setState({ lastExtensionEventAt: t0 })
      render(<ExtensionStatus />)
      expect(screen.queryByText('No draft data yet')).not.toBeInTheDocument()
      expect(screen.getByText('0s ago')).toBeInTheDocument()

      // Nothing further arrives; the label must stay truthful on its own.
      act(() => { vi.advanceTimersByTime(30_000) })
      expect(screen.getByText('30s ago')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does NOT warn merely because a platform went quiet', () => {
    // Sleeper only emits on a real pick and Yahoo only on a change, so a long
    // gap is normal. Warning here would fire during healthy drafts.
    vi.useFakeTimers()
    try {
      useDraftStore.setState({ lastExtensionEventAt: Date.now() })
      render(<ExtensionStatus />)
      act(() => { vi.advanceTimersByTime(5 * 60_000) })
      expect(screen.queryByText('No draft data yet')).not.toBeInTheDocument()
      expect(screen.getByText('5m ago')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('ExtensionBlockedBanner — name the reason instead of sitting still', () => {
  it('renders nothing when nothing is wrong', () => {
    const { container } = render(<ExtensionBlockedBanner />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the server-supplied reason as an alert', () => {
    useDraftStore.setState({
      extensionBlocked: {
        code: 'live_draft_requires_paid_plan',
        message: 'Your Rook extension is sending draft updates, but this account’s plan does not include live draft, so they are being refused.',
      },
    })
    render(<ExtensionBlockedBanner />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/does not include live draft/)).toBeInTheDocument()
    // A plan problem is actionable — offer the way to fix it.
    expect(screen.getByRole('link', { name: /see plans/i })).toHaveAttribute('href', '/pricing')
  })

  it('offers no plan link for a reason that is not about the plan', () => {
    useDraftStore.setState({
      extensionBlocked: { code: 'something_else', message: 'Updates are being refused.' },
    })
    render(<ExtensionBlockedBanner />)
    expect(screen.getByText('Updates are being refused.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /see plans/i })).not.toBeInTheDocument()
  })
})

describe('draft store — the two signals stay consistent', () => {
  it('an arriving update clears a standing refusal notice', () => {
    // Leaving the warning up after updates resume would state something untrue.
    useDraftStore.setState({
      extensionBlocked: { code: 'live_draft_requires_paid_plan', message: 'refused' },
    })
    act(() => { useDraftStore.getState().markExtensionEvent(1_700_000_000_000) })

    expect(useDraftStore.getState().extensionBlocked).toBeNull()
    expect(useDraftStore.getState().lastExtensionEventAt).toBe(1_700_000_000_000)
  })

  it('records the arrival time when nothing was wrong', () => {
    act(() => { useDraftStore.getState().markExtensionEvent(1_700_000_000_000) })
    expect(useDraftStore.getState().lastExtensionEventAt).toBe(1_700_000_000_000)
    expect(useDraftStore.getState().extensionBlocked).toBeNull()
  })
})
