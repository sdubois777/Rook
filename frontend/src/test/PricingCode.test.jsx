/**
 * Promo code entry on the checkout path.
 *
 * Two rules the UI has to hold, both of which the backend enforces anyway — the
 * point here is that the user is told, rather than being bounced by a 400 after
 * they have committed to buying:
 *
 *   1. a code is checked before the redirect to Stripe;
 *   2. a code never rides along with a season interval.
 */
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
vi.mock('../api/referral', () => ({ validateCode: vi.fn() }))
vi.mock('../hooks/usePricing', () => ({ usePricing: () => pricingHookValue() }))

import PricingTable from '../components/landing/PricingTable'
import { createCheckout, redirectTo } from '../api/billing'
import { validateCode } from '../api/referral'

const VALID = { valid: true, percent_off: 25, message: '25% off your first month.' }
const INVALID = { valid: false, percent_off: 0, message: 'That code is not valid.' }

function renderTable() {
  return render(
    <MemoryRouter>
      <PricingTable />
    </MemoryRouter>
  )
}

function typeCode(value) {
  fireEvent.change(screen.getByLabelText(/referral or welcome code/i), {
    target: { value },
  })
}

describe('PricingTable discount code', () => {
  beforeEach(() => {
    createCheckout.mockClear()
    redirectTo.mockClear()
    validateCode.mockReset()
    localStorage.clear()
    h.signedIn = true
  })

  it('pre-fills from a code captured off a ?ref= link', () => {
    localStorage.setItem('referralCode', 'ROOK-7K2M9X')
    renderTable()
    expect(screen.getByLabelText(/referral or welcome code/i)).toHaveValue('ROOK-7K2M9X')
  })

  it('shows the message the server returns for a code', async () => {
    validateCode.mockResolvedValue(VALID)
    renderTable()
    typeCode('ROOK-7K2M9X')
    fireEvent.click(screen.getByRole('button', { name: /check/i }))

    expect(await screen.findByText(VALID.message)).toBeInTheDocument()
    expect(validateCode).toHaveBeenCalledWith('ROOK-7K2M9X', 'monthly')
  })

  it('validates before redirecting and passes the code to checkout', async () => {
    validateCode.mockResolvedValue(VALID)
    renderTable()
    typeCode('rook-7k2m9x')
    fireEvent.click(screen.getByRole('button', { name: /Monthly — \$8\/mo/i }))

    await waitFor(() =>
      // Upper-cased to match the server's own normalization.
      expect(createCheckout).toHaveBeenCalledWith('standard', 'monthly', 'ROOK-7K2M9X')
    )
    expect(validateCode).toHaveBeenCalledWith('ROOK-7K2M9X', 'monthly')
  })

  it('does not start checkout when the code is rejected', async () => {
    validateCode.mockResolvedValue(INVALID)
    renderTable()
    typeCode('ROOK-NOPE99')
    fireEvent.click(screen.getByRole('button', { name: /Monthly — \$8\/mo/i }))

    expect(await screen.findByText(INVALID.message)).toBeInTheDocument()
    expect(createCheckout).not.toHaveBeenCalled()
    expect(redirectTo).not.toHaveBeenCalled()
  })

  it('disables the season buttons while a code is entered, and says why', () => {
    renderTable()
    typeCode('ROOK-7K2M9X')

    screen.getAllByRole('button', { name: /Season pass/i }).forEach((b) => {
      expect(b).toBeDisabled()
    })
    expect(screen.getByText(/monthly plans only/i)).toBeInTheDocument()
  })

  it('leaves the season buttons alone when no code is entered', () => {
    renderTable()
    screen.getAllByRole('button', { name: /Season pass/i }).forEach((b) => {
      expect(b).toBeEnabled()
    })
  })

  it('sends no code and checks nothing when the input is empty', async () => {
    renderTable()
    fireEvent.click(screen.getByRole('button', { name: /Monthly — \$8\/mo/i }))

    await waitFor(() =>
      expect(createCheckout).toHaveBeenCalledWith('standard', 'monthly', '')
    )
    expect(validateCode).not.toHaveBeenCalled()
  })

  it('hides the input when signed out — there is no checkout to attach it to', () => {
    h.signedIn = false
    renderTable()
    expect(screen.queryByLabelText(/referral or welcome code/i)).not.toBeInTheDocument()
  })
})
