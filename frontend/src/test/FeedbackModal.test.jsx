import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/feedback', () => ({
  submitFeedback: vi.fn(async () => ({
    issue_number: 437,
    issue_url: 'https://github.com/sdubois777/Rook/issues/437',
  })),
  fetchFeedbackStatus: vi.fn(async () => ({ enabled: true })),
}))

vi.mock('../context/LeagueContext', () => ({
  useLeague: () => ({
    selectedLeague: { platform: 'sleeper' },
    draftType: 'auction',
    scoringFormat: 'half_ppr',
  }),
}))

import FeedbackModal from '../components/FeedbackModal'
import { submitFeedback } from '../api/feedback'

function renderModal(props = {}) {
  return render(
    <MemoryRouter initialEntries={['/draftboard?x=1']}>
      <FeedbackModal onClose={() => {}} {...props} />
    </MemoryRouter>
  )
}

function fillValidReport() {
  fireEvent.change(screen.getByPlaceholderText(/Gap column shows the wrong number/i), {
    target: { value: 'Gap column is wrong' },
  })
  fireEvent.change(screen.getByPlaceholderText(/What did you do/i), {
    target: { value: 'On half-PPR the gap does not match the ceiling shown.' },
  })
}

describe('FeedbackModal', () => {
  beforeEach(() => submitFeedback.mockClear())

  it('opens on an empty form — a previous submission never bleeds through', () => {
    const { unmount } = renderModal()
    fillValidReport()
    unmount()          // App unmounts it on close, which is what resets the form
    renderModal()
    expect(screen.getByPlaceholderText(/Gap column shows the wrong number/i)).toHaveValue('')
    expect(screen.getByRole('button', { name: /Send report/i })).toBeDisabled()
  })

  it('will not submit an empty or too-short report', () => {
    renderModal()
    expect(screen.getByRole('button', { name: /Send report/i })).toBeDisabled()
  })

  it('sends the report with the page and league context attached', async () => {
    renderModal()
    fillValidReport()
    fireEvent.click(screen.getByRole('button', { name: /Send report/i }))

    await waitFor(() => expect(submitFeedback).toHaveBeenCalledTimes(1))
    const payload = submitFeedback.mock.calls[0][0]
    expect(payload.kind).toBe('bug')
    expect(payload.title).toBe('Gap column is wrong')
    expect(payload.page).toBe('/draftboard?x=1')
    expect(payload.league_platform).toBe('sleeper')
    expect(payload.draft_type).toBe('auction')
    expect(payload.scoring_format).toBe('half_ppr')
    expect(payload.viewport).toMatch(/^\d+x\d+$/)
  })

  it('switching to a suggestion changes the report kind', async () => {
    renderModal()
    fireEvent.click(screen.getByRole('button', { name: /I have a suggestion/i }))
    fireEvent.change(screen.getByPlaceholderText(/Show bye weeks/i), {
      target: { value: 'Show bye weeks' },
    })
    fireEvent.change(screen.getByPlaceholderText(/What would you like Rook to do/i), {
      target: { value: 'It would help to see bye weeks next to each player.' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Send report/i }))
    await waitFor(() => expect(submitFeedback.mock.calls[0][0].kind).toBe('idea'))
  })

  it('confirms with the issue number and says it will be investigated first', async () => {
    renderModal()
    fillValidReport()
    fireEvent.click(screen.getByRole('button', { name: /Send report/i }))
    await waitFor(() =>
      expect(screen.getByText(/issue #437 in the Rook backlog/i)).toBeInTheDocument()
    )
    expect(
      screen.getByText(/reproduced and investigated before anything is changed/i)
    ).toBeInTheDocument()
  })

  it('shows the server message when filing fails, and does not claim success', async () => {
    submitFeedback.mockRejectedValueOnce({
      response: { data: { message: "Bug reporting isn't set up on this deployment yet." } },
    })
    renderModal()
    fillValidReport()
    fireEvent.click(screen.getByRole('button', { name: /Send report/i }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/isn't set up/i)
    )
    expect(screen.queryByText(/Report sent/i)).not.toBeInTheDocument()
  })

  it('tells the user their email is not sent', () => {
    renderModal()
    expect(screen.getByText(/Your email address is not sent/i)).toBeInTheDocument()
  })
})
