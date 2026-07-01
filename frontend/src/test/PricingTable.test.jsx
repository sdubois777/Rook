import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ signedIn: true }))

vi.mock('@clerk/clerk-react', () => ({ useAuth: () => ({ isSignedIn: h.signedIn }) }))
vi.mock('../api/billing', () => ({
  createCheckout: vi.fn(async () => 'https://checkout.stripe.com/x'),
  redirectTo: vi.fn(),
}))

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

  it('signed-in: a tier CTA starts checkout for that tier and redirects', async () => {
    renderTable()
    // "Start Trial" appears on Standard + Pro; the first is Standard.
    const buttons = screen.getAllByRole('button', { name: /Start Trial/i })
    fireEvent.click(buttons[0])
    await waitFor(() => expect(createCheckout).toHaveBeenCalledWith('standard'))
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith('https://checkout.stripe.com/x')
    )
  })

  it('signed-out: CTAs are sign-up links, not checkout', () => {
    h.signedIn = false
    renderTable()
    expect(screen.queryByRole('button', { name: /Start Trial/i })).not.toBeInTheDocument()
    const links = screen.getAllByRole('link', { name: /Get Started|Start Trial/i })
    expect(links.length).toBeGreaterThan(0)
    links.forEach((l) => expect(l).toHaveAttribute('href', '/sign-up'))
    expect(createCheckout).not.toHaveBeenCalled()
  })
})
