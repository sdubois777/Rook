import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/billing', () => ({
  createPackCheckout: vi.fn(async () => 'https://checkout.stripe.com/pack'),
  redirectTo: vi.fn(),
}))

import BuyCreditsCard from '../components/billing/BuyCreditsCard'
import { createPackCheckout, redirectTo } from '../api/billing'

describe('BuyCreditsCard', () => {
  beforeEach(() => {
    createPackCheckout.mockClear()
    redirectTo.mockClear()
  })

  it('renders the three packs with credit amounts', () => {
    render(<BuyCreditsCard />)
    expect(screen.getByText('75 cr')).toBeInTheDocument()
    expect(screen.getByText('175 cr')).toBeInTheDocument()
    expect(screen.getByText('500 cr')).toBeInTheDocument()
  })

  it('clicking a pack starts checkout for that pack and redirects', async () => {
    render(<BuyCreditsCard />)
    fireEvent.click(screen.getByText('175 cr'))
    await waitFor(() => expect(createPackCheckout).toHaveBeenCalledWith('medium'))
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith('https://checkout.stripe.com/pack')
    )
  })
})
