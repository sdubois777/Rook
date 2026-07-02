import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/billing', () => ({
  previewChangePlan: vi.fn(),
  confirmChangePlan: vi.fn(),
}))

import ChangePlanCard from '../components/billing/ChangePlanCard'
import { previewChangePlan, confirmChangePlan } from '../api/billing'

describe('ChangePlanCard', () => {
  beforeEach(() => {
    previewChangePlan.mockReset()
    confirmChangePlan.mockReset()
  })

  it('upgrade: previews a charge line, confirm reuses proration_date + calls onApplied', async () => {
    previewChangePlan.mockResolvedValue({
      direction: 'upgrade', amount_due_today: 912, currency: 'usd',
      effective: 'now', proration_date: 111, target_tier: 'pro',
    })
    confirmChangePlan.mockResolvedValue({ status: 'applied', effective: 'now', target_tier: 'pro' })
    const onApplied = vi.fn()

    render(<ChangePlanCard currentTier="standard" onApplied={onApplied} />)

    fireEvent.click(screen.getByRole('button', { name: /Change to.*Pro/i }))
    await waitFor(() => expect(previewChangePlan).toHaveBeenCalledWith('pro'))
    // charge shown from server amount (cents -> $9.12), never computed here
    expect(screen.getByText(/\$9\.12/)).toBeInTheDocument()
    expect(screen.getByText(/charged/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Confirm/i }))
    await waitFor(() => expect(confirmChangePlan).toHaveBeenCalledWith('pro', 111))
    await waitFor(() => expect(onApplied).toHaveBeenCalled())
    expect(await screen.findByText(/Upgraded to/i)).toBeInTheDocument()
  })

  it('downgrade: previews a scheduled line, confirm shows scheduled (no onApplied poll)', async () => {
    previewChangePlan.mockResolvedValue({
      direction: 'downgrade', amount_due_today: 0, currency: 'usd',
      effective: '2026-08-01T00:00:00+00:00', proration_date: null, target_tier: 'standard',
    })
    confirmChangePlan.mockResolvedValue({
      status: 'scheduled', effective: '2026-08-01T00:00:00+00:00', target_tier: 'standard',
    })
    const onApplied = vi.fn()

    render(<ChangePlanCard currentTier="pro" onApplied={onApplied} />)

    fireEvent.click(screen.getByRole('button', { name: /Change to.*Standard/i }))
    await waitFor(() => expect(previewChangePlan).toHaveBeenCalledWith('standard'))
    expect(screen.getByText(/No charge today/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Confirm/i }))
    await waitFor(() => expect(confirmChangePlan).toHaveBeenCalledWith('standard', null))
    expect(await screen.findByText(/Scheduled:/i)).toBeInTheDocument()
    expect(onApplied).not.toHaveBeenCalled()
  })

  it('downgrade over the active-league cap shows the chooser warning', async () => {
    previewChangePlan.mockResolvedValue({
      direction: 'downgrade', amount_due_today: 0, currency: 'usd',
      effective: '2026-08-01T00:00:00+00:00', proration_date: null,
      target_tier: 'standard', active_leagues: 3, max_active_leagues: 2,
    })
    render(<ChangePlanCard currentTier="pro" onApplied={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /Change to.*Standard/i }))
    await waitFor(() => expect(previewChangePlan).toHaveBeenCalledWith('standard'))
    expect(screen.getByText(/allows 2 active leagues; you have 3/i)).toBeInTheDocument()
    expect(screen.getByText(/choose which stay active/i)).toBeInTheDocument()
  })

  it('downgrade within the cap shows no chooser warning', async () => {
    previewChangePlan.mockResolvedValue({
      direction: 'downgrade', amount_due_today: 0, currency: 'usd',
      effective: '2026-08-01T00:00:00+00:00', proration_date: null,
      target_tier: 'standard', active_leagues: 1, max_active_leagues: 2,
    })
    render(<ChangePlanCard currentTier="pro" onApplied={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /Change to.*Standard/i }))
    await waitFor(() => expect(previewChangePlan).toHaveBeenCalledWith('standard'))
    expect(screen.queryByText(/choose which stay active/i)).not.toBeInTheDocument()
  })

  it('offers only the tiers other than the current one', () => {
    render(<ChangePlanCard currentTier="pro" onApplied={() => {}} />)
    expect(screen.getByRole('button', { name: /Change to.*Intro/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Change to.*Standard/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Change to.*Pro/i })).not.toBeInTheDocument()
  })
})
