import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/billing', () => ({
  createCheckout: vi.fn(async () => 'https://checkout.stripe.com/x'),
  createPackCheckout: vi.fn(async () => 'https://checkout.stripe.com/pack'),
  createPortal: vi.fn(async () => 'https://billing.stripe.com/p'),
  redirectTo: vi.fn(),
}))

import BillingNotice from '../components/BillingNotice'
import { createCheckout, createPackCheckout, redirectTo } from '../api/billing'

describe('BillingNotice', () => {
  beforeEach(() => {
    createCheckout.mockClear()
    createPackCheckout.mockClear()
    redirectTo.mockClear()
  })

  it('renders nothing until a billing event fires', () => {
    render(<BillingNotice />)
    expect(screen.queryByText('Upgrade required')).not.toBeInTheDocument()
  })

  it('feature-required event → upgrade prompt → checkout for the required tier', async () => {
    render(<BillingNotice />)
    act(() => {
      window.dispatchEvent(
        new CustomEvent('billing:feature-required', { detail: { required_tier: 'standard' } })
      )
    })
    expect(screen.getByText('Upgrade required')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Upgrade to standard/i }))
    await waitFor(() => expect(createCheckout).toHaveBeenCalledWith('standard'))
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith('https://checkout.stripe.com/x')
    )
  })

  it('insufficient-credits event → out-of-credits prompt → pack checkout', async () => {
    render(<BillingNotice />)
    act(() => {
      window.dispatchEvent(
        new CustomEvent('billing:insufficient-credits', { detail: { required: 10, available: 3 } })
      )
    })
    expect(screen.getByText('Out of credits')).toBeInTheDocument()
    expect(screen.getByText(/need 10 credits but have 3/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Buy credits/i }))
    await waitFor(() => expect(createPackCheckout).toHaveBeenCalledWith('small'))
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith('https://checkout.stripe.com/pack')
    )
  })

  it('can be dismissed', () => {
    render(<BillingNotice />)
    act(() => {
      window.dispatchEvent(
        new CustomEvent('billing:feature-required', { detail: { required_tier: 'pro' } })
      )
    })
    expect(screen.getByText('Upgrade required')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(screen.queryByText('Upgrade required')).not.toBeInTheDocument()
  })
})
