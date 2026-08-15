/**
 * The referral panel on the account page.
 *
 * Every percentage it prints comes from an API response — /account/referral for
 * the user's own earned rate and cap, /billing/pricing for what their friend
 * gets. A number written into the component would be a second definition of the
 * program, free to drift from backend/models/user.py.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { pricingHookValue, PRICING_FIXTURE } from './pricingMock'

const h = vi.hoisted(() => ({ referral: null, referralFails: false }))

vi.mock('@clerk/clerk-react', () => ({
  useUser: () => ({
    isLoaded: true,
    user: { primaryEmailAddress: { emailAddress: 'me@example.com' } },
  }),
  useClerk: () => ({ signOut: vi.fn() }),
}))
vi.mock('../api/billing', () => ({
  createPortal: vi.fn(),
  redirectTo: vi.fn(),
}))
vi.mock('../hooks/usePricing', () => ({ usePricing: () => pricingHookValue() }))
vi.mock('../context/LeagueContext', () => ({
  useLeague: () => ({ selectedLeague: null, setSelectedLeague: vi.fn() }),
}))
// Sibling account-page cards, each with their own fetches. Not under test here.
vi.mock('../components/billing/LeagueChooser', () => ({ default: () => null }))
vi.mock('../components/billing/BuyCreditsCard', () => ({ default: () => null }))
vi.mock('../components/billing/ChangePlanCard', () => ({ default: () => null }))
vi.mock('../components/landing/LandingFooter', () => ({ default: () => null }))

vi.mock('../api/client', () => {
  const responses = {
    '/account/me': {
      id: 'u1', email: 'me@example.com', display_name: 'Me', tier: 'standard',
      credits_remaining: 0, tier_limits: {}, subscription_status: 'active',
      tier_expires_at: null,
    },
    '/account/credits': {
      balance: 0, monthly_allowance: 0, usage_last_30_days: 0, history: [],
    },
    '/account/leagues': [],
    '/account/draft-token': { draft_token: 'tok-123' },
    '/account/credentials': { platforms: [] },
  }
  return {
    apiClient: {
      get: vi.fn(async (url) => {
        if (url === '/account/referral') {
          if (h.referralFails) throw new Error('boom')
          return { data: h.referral }
        }
        return { data: responses[url] }
      }),
      post: vi.fn(async () => ({ data: {} })),
      delete: vi.fn(async () => ({ data: {} })),
    },
  }
})

import AccountPage from '../pages/Account'
import { apiClient } from '../api/client'

// Deliberately NOT the production rates: a panel that renders these numbers is
// reading the API, and one that renders the real ones has them written into it.
const STATE = {
  code: 'ROOK-7K2M9X',
  share_url: 'https://rookff.com/?ref=ROOK-7K2M9X',
  referral_count: 3,
  percent_off: 12,
  percent_off_cap: 40,
  percent_off_per_referral: 4,
  eligible: true,
}

function renderAccount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AccountPage />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('account referral panel', () => {
  beforeEach(() => {
    h.referral = { ...STATE }
    h.referralFails = false
    apiClient.get.mockClear()
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn() },
      configurable: true,
    })
  })

  it('shows the code and the counts and rates the API returned', async () => {
    renderAccount()

    expect(await screen.findByText('ROOK-7K2M9X')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()          // referrals counting
    expect(screen.getByText('12% off')).toBeInTheDocument()    // earned right now
    // Remaining headroom, derived from the API's own cap and per-referral rate.
    expect(screen.getByText(/28% more is available/)).toBeInTheDocument()
    expect(screen.getByText(/each referral adds 4%/)).toBeInTheDocument()
    expect(screen.getByText(/up to the 40% cap/)).toBeInTheDocument()
  })

  it("renders the friend's discount from the pricing sheet, not a literal", async () => {
    renderAccount()
    const pct = PRICING_FIXTURE.referral.referred_percent_off
    expect(
      await screen.findByText(new RegExp(`${pct}% off their first month`))
    ).toBeInTheDocument()
  })

  it('copies the share URL', async () => {
    renderAccount()
    fireEvent.click(await screen.findByRole('button', { name: 'Copy link' }))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(STATE.share_url)
    expect(await screen.findByRole('button', { name: 'Copied!' })).toBeInTheDocument()
  })

  it('says plainly when an earned discount is not being applied', async () => {
    h.referral = { ...STATE, eligible: false }
    renderAccount()
    expect(
      await screen.findByText(/not being applied right now/i)
    ).toBeInTheDocument()
  })

  it('says nothing about eligibility when the discount is live', async () => {
    renderAccount()
    await screen.findByText('ROOK-7K2M9X')
    expect(screen.queryByText(/not being applied right now/i)).not.toBeInTheDocument()
  })

  it('reports the cap once it is reached', async () => {
    h.referral = { ...STATE, referral_count: 10, percent_off: STATE.percent_off_cap }
    renderAccount()
    expect(await screen.findByText(/at the 40% cap/)).toBeInTheDocument()
  })

  it('hides the panel when the referral call fails, and still renders the page', async () => {
    h.referralFails = true
    renderAccount()
    await waitFor(() => expect(screen.getByText('My Account')).toBeInTheDocument())
    expect(screen.queryByText('ROOK-7K2M9X')).not.toBeInTheDocument()
  })
})
