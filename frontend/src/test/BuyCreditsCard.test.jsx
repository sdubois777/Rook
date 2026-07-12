import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/billing', () => ({
  createPackCheckout: vi.fn(async () => 'https://checkout.stripe.com/pack'),
  redirectTo: vi.fn(),
}))
vi.mock('../hooks/usePricing', () => ({ usePricing: () => pricingHookValue() }))

import { pricingHookValue } from './pricingMock'
import BuyCreditsCard from '../components/billing/BuyCreditsCard'
import { createPackCheckout, redirectTo } from '../api/billing'

describe('BuyCreditsCard', () => {
  beforeEach(() => {
    createPackCheckout.mockClear()
    redirectTo.mockClear()
  })

  it('renders the single pack from the fetched pricing sheet', () => {
    render(<BuyCreditsCard />)
    expect(screen.getByText('100 cr')).toBeInTheDocument()
  })

  it('clicking a pack starts checkout for that pack and redirects', async () => {
    render(<BuyCreditsCard />)
    fireEvent.click(screen.getByText('100 cr'))
    await waitFor(() => expect(createPackCheckout).toHaveBeenCalledWith('credits_100'))
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith('https://checkout.stripe.com/pack')
    )
  })
})
