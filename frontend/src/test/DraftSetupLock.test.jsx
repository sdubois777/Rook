import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ limits: null }))

vi.mock('../hooks/useEntitlements', () => ({
  useEntitlements: () => ({ tierLimits: h.limits }),
  isFeatureLocked: (limits, f) => (limits ? limits[f] === false : false),
}))

import DraftSetup from '../components/draft/DraftSetup'

function renderSetup() {
  return render(
    <MemoryRouter>
      <DraftSetup />
    </MemoryRouter>
  )
}

describe('DraftSetup live_draft lock affordance', () => {
  beforeEach(() => {
    h.limits = null
  })

  it('intro (live_draft:false) shows a locked state + upgrade link, no Start button', () => {
    h.limits = { live_draft: false }
    renderSetup()
    expect(screen.getByText(/Live draft is a/i)).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /Upgrade to unlock/i })
    expect(link).toHaveAttribute('href', '/account')
    expect(screen.queryByRole('button', { name: /Start Draft/i })).not.toBeInTheDocument()
  })

  it('standard (live_draft:true) shows the normal Start button', () => {
    h.limits = { live_draft: true }
    renderSetup()
    expect(screen.getByRole('button', { name: /Start Draft/i })).toBeInTheDocument()
    expect(screen.queryByText(/Live draft is a/i)).not.toBeInTheDocument()
  })

  it('unknown entitlements fail open to the normal Start button', () => {
    h.limits = null
    renderSetup()
    expect(screen.getByRole('button', { name: /Start Draft/i })).toBeInTheDocument()
  })
})
