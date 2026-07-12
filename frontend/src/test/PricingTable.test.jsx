import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { pricingHookValue } from './pricingMock'

const h = vi.hoisted(() => ({ signedIn: true }))

vi.mock('@clerk/clerk-react', () => ({ useAuth: () => ({ isSignedIn: h.signedIn }) }))
vi.mock('../api/billing', () => ({
  createCheckout: vi.fn(async () => 'https://checkout.stripe.com/x'),
  redirectTo: vi.fn(),
}))
vi.mock('../hooks/usePricing', () => ({ usePricing: () => pricingHookValue() }))

import PricingTable from '../components/landing/PricingTable'
import { createCheckout, redirectTo } from '../api/billing'

function renderTable() {
  return render(
    <MemoryRouter>
      <PricingTable />
    </MemoryRouter>
  )
}

describe('PricingTable CTAs', () => {
  beforeEach(() => {
    createCheckout.mockClear()
    redirectTo.mockClear()
    h.signedIn = true
  })

  it('renders prices from the fetched sheet (never hardcoded)', () => {
    renderTable()
    expect(screen.getByText('$8')).toBeInTheDocument()      // standard monthly
    expect(screen.getByText('$18')).toBeInTheDocument()     // pro monthly
    expect(screen.getByText(/\$29\/season/)).toBeInTheDocument()
    expect(screen.getByText(/\$59\/season/)).toBeInTheDocument()
    expect(screen.getByText(/30 credits at signup/)).toBeInTheDocument()
  })

  it('signed-in: monthly CTA starts a monthly checkout and redirects', async () => {
    renderTable()
    fireEvent.click(screen.getByRole('button', { name: /Monthly — \$8\/mo/i }))
    await waitFor(() => expect(createCheckout).toHaveBeenCalledWith('standard', 'monthly'))
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith('https://checkout.stripe.com/x')
    )
  })

  it('signed-in: season CTA starts a season checkout', async () => {
    renderTable()
    fireEvent.click(screen.getByRole('button', { name: /Season pass — \$59/i }))
    await waitFor(() => expect(createCheckout).toHaveBeenCalledWith('pro', 'season'))
  })

  it('signed-out: CTAs are sign-up links, not checkout', () => {
    h.signedIn = false
    renderTable()
    expect(screen.queryByRole('button', { name: /Monthly/i })).not.toBeInTheDocument()
    const links = screen.getAllByRole('link')
    expect(links.length).toBeGreaterThan(0)
    links.forEach((l) => expect(l).toHaveAttribute('href', '/sign-up'))
    expect(createCheckout).not.toHaveBeenCalled()
  })
})
