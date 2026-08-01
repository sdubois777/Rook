import { describe, it, expect } from 'vitest'
import { waiverBudgetSummary, waiverSystemLabel } from '../lib/waiverSettings'

/**
 * The rules that decide whether a dollar figure may appear on the waiver page.
 *
 * The behaviour being locked down: a budget NUMBER never authorises a budget
 * CLAIM. Only the league's waiver system does. Every platform ships a budget value
 * (almost always 100) even on leagues that never bid, so several cases below pass a
 * budget of 100 alongside a non-bidding league and require that nothing is said.
 */
describe('waiverBudgetSummary — when a dollar figure is allowed', () => {
  it('states the real budget and balance when both were read', () => {
    const s = waiverBudgetSummary({
      usesBiddingBudget: true, budget: 200, remaining: 137,
    })
    expect(s.text).toBe('$137 of $200 budget left')
    expect(s.isAssumption).toBe(false)
  })

  it('says NOTHING about money for a league that does not bid, even when a budget is supplied', () => {
    const s = waiverBudgetSummary({
      usesBiddingBudget: false, budget: 100, remaining: 100, waiverPosition: 3,
    })
    expect(s.text).toBe('waiver priority #3')
    expect(s.text).not.toContain('$')
    expect(s.kind).toBe('no_bidding')
  })

  it('says nothing at all for a non-bidding league whose waiver order is unknown', () => {
    const s = waiverBudgetSummary({ usesBiddingBudget: false, budget: 100 })
    expect(s.text).toBeNull()
  })

  it('claims nothing when the waiver system itself could not be read', () => {
    // Budget present, system unknown. This is the exact shape the fabricated $100
    // came from, and it must produce no clause.
    expect(waiverBudgetSummary({ budget: 100, remaining: 100 }).text).toBeNull()
    expect(waiverBudgetSummary({}).text).toBeNull()
    expect(waiverBudgetSummary().text).toBeNull()
  })

  it('keeps "does not bid" and "we do not know" as different answers', () => {
    // Both produce no clause when there is nothing else to show, so asserting only
    // on the text cannot tell them apart — and a truthiness test on the three-state
    // value collapses them without breaking a text-only assertion. The distinction
    // is real: one means a dollar figure is meaningless, the other means we have
    // not earned the right to say either way.
    expect(waiverBudgetSummary({ usesBiddingBudget: false }).kind).toBe('no_bidding')
    expect(waiverBudgetSummary({ usesBiddingBudget: null }).kind).toBe('unknown')
    expect(waiverBudgetSummary({ usesBiddingBudget: undefined }).kind).toBe('unknown')
  })

  it('never shows a waiver order position for a league whose system is unknown', () => {
    // A waiver position is itself a claim about how the league works.
    const s = waiverBudgetSummary({ usesBiddingBudget: null, waiverPosition: 3 })
    expect(s.kind).toBe('unknown')
    expect(s.text).toBeNull()
  })

  it('an assumed amount never implies the league bids', () => {
    // The backend produces a $100 figure to rank with even when the waiver system
    // is unknown. Stating it here would assert that the league bids — the one claim
    // only the waiver-system field may authorise. The panel below the cards is
    // where that case is explained in full.
    const s = waiverBudgetSummary({
      usesBiddingBudget: null, remaining: 100, budgetIsAssumed: true,
      budgetBasis: 'unknown_system',
    })
    expect(s.kind).toBe('unknown')
    expect(s.text).toBeNull()
  })

  it('distinguishes an unknown SPEND from an unknown BUDGET', () => {
    // Budget read, spend not: state the budget, never a balance.
    const spend = waiverBudgetSummary({
      usesBiddingBudget: true, budget: 200, remaining: 200,
      budgetIsAssumed: true, budgetBasis: 'full_budget',
    })
    expect(spend.text).toBe('$200 budget, spend unknown')
    expect(spend.isAssumption).toBe(true)
    expect(spend.text).not.toContain('left')

    // Nothing read: say the amount is assumed.
    const std = waiverBudgetSummary({
      usesBiddingBudget: true, budget: null, remaining: 100,
      budgetIsAssumed: true, budgetBasis: 'standard_budget',
    })
    expect(std.text).toBe('assuming a $100 budget')
  })

  it('prints no figure at all rather than "$NaN" or a bare "$"', () => {
    for (const bad of [NaN, undefined, null, '137', {}]) {
      const s = waiverBudgetSummary({ usesBiddingBudget: true, budget: 200, remaining: bad })
      expect(s.text).not.toMatch(/NaN|\$\s*$|\$undefined|\$null|\$\[object/)
      expect(s.text).toBe('$200 budget')     // balance withheld, budget still stated
    }
    const bothBad = waiverBudgetSummary({ usesBiddingBudget: true, budget: NaN, remaining: NaN })
    expect(bothBad.text).toBe('budget unavailable')
  })

  it('marks an assumed figure as an assumption', () => {
    const s = waiverBudgetSummary({
      usesBiddingBudget: true, budget: null, remaining: 100, budgetIsAssumed: true,
    })
    expect(s.isAssumption).toBe(true)
    expect(s.text).toBe('assuming a $100 budget')
  })

  it('an assumption is never presented as the league\'s own figure', () => {
    const assumed = waiverBudgetSummary({
      usesBiddingBudget: true, remaining: 100, budgetIsAssumed: true,
    })
    const real = waiverBudgetSummary({
      usesBiddingBudget: true, budget: 100, remaining: 100,
    })
    expect(assumed.text).not.toBe(real.text)
    expect(assumed.isAssumption).not.toBe(real.isAssumption)
  })

  it('a bidding league with an unknown balance shows the budget, not a made-up balance', () => {
    const s = waiverBudgetSummary({ usesBiddingBudget: true, budget: 200 })
    expect(s.text).toBe('$200 budget')
    expect(s.text).not.toContain('left')     // no balance was read, so none is stated
  })

  it('a bidding league with no readable budget says so instead of naming a number', () => {
    const s = waiverBudgetSummary({ usesBiddingBudget: true })
    expect(s.text).toBe('budget unavailable')
    expect(s.text).not.toContain('$')
  })

  it('treats a zero balance as a real answer, not a missing one', () => {
    const s = waiverBudgetSummary({ usesBiddingBudget: true, budget: 100, remaining: 0 })
    expect(s.text).toBe('$0 of $100 budget left')
  })
})

describe('waiverSystemLabel', () => {
  it('keeps short platform codes upper-case and sentence-cases readable labels', () => {
    expect(waiverSystemLabel('faab')).toBe('FAAB')
    expect(waiverSystemLabel('budget')).toBe('Budget')
    expect(waiverSystemLabel('rolling priority')).toBe('Rolling priority')
    expect(waiverSystemLabel('continuous waiver priority')).toBe('Continuous waiver priority')
  })

  it('returns null when there is no system to name', () => {
    expect(waiverSystemLabel(null)).toBeNull()
    expect(waiverSystemLabel(undefined)).toBeNull()
    expect(waiverSystemLabel('')).toBeNull()
    expect(waiverSystemLabel('   ')).toBeNull()
  })
})
