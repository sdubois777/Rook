/**
 * How to describe a league's waiver settings on screen — in ONE place, because
 * getting it wrong prints a dollar figure the customer's league does not have.
 *
 * This page used to render `$100 of $100 budget left` for every customer, from a
 * budget that was never read from their league. Three rules follow from that, and
 * all three are enforced here rather than in JSX:
 *
 *   1. `usesBiddingBudget` is the ONLY thing that may authorise a dollar figure.
 *      It is three-state: true = the league bids, false = the league claims by
 *      waiver priority or reverse standings, null/undefined = we could not read
 *      the league's waiver system. Never infer it from the presence of a budget
 *      number — every platform ships a budget value (almost always 100) even on
 *      leagues that never bid, which is precisely where the fake $100 came from.
 *      It is checked BEFORE anything else, including before any assumption, so an
 *      assumed amount can never imply that a league bids.
 *
 *   2. A figure we assumed is never shown as a figure we read. `budgetBasis` says
 *      what, if anything, was assumed; the returned descriptor carries
 *      `isAssumption`, and the caller must say so on screen.
 *
 *   3. Only a real, finite number is ever printed. A malformed payload produces no
 *      clause rather than "$NaN" or a bare "$".
 *
 * An unknown balance is left absent. It is NOT backfilled with the league budget:
 * "we don't know what you've spent" and "you have spent nothing" are different
 * statements, and the old code made the first one silently print as the second.
 */

/** A number we are willing to print. Rejects null, undefined, NaN and non-numbers.
 *  Exported so the recommendation cards apply the identical rule. */
export function isPrintableNumber(v) {
  return typeof v === 'number' && Number.isFinite(v)
}
const isNum = isPrintableNumber

/** Human label for the league's waiver system, or null when there is none to show. */
export function waiverSystemLabel(type) {
  const t = String(type ?? '').trim()
  if (!t) return null
  // Short platform codes read badly sentence-cased; readable labels read badly
  // shouted ("CONTINUOUS WAIVER PRIORITY").
  if (t.toLowerCase() === 'faab') return 'FAAB'
  return t.charAt(0).toUpperCase() + t.slice(1)
}

/**
 * The budget/priority clause for a league.
 *
 * Returns { kind, text, isAssumption }. `text` is null when there is nothing
 * truthful to say, and the caller renders no clause at all.
 *
 *   known            — the league bids and we read both the budget and the balance
 *   spend_unknown    — budget read, this team's spend was not, so no balance is stated
 *   assumed          — a stated assumption, NOT the league's own figure
 *   budget_unknown   — the league bids but no amount could be read
 *   no_bidding       — the league does not bid; a dollar figure is meaningless
 *   unknown          — the waiver system itself could not be read; claim nothing
 *
 * `budgetBasis` is the backend's word for what was assumed ("league", "full_budget",
 * "standard_budget", "unknown_system", "no_bidding"); `budgetIsAssumed` is the older
 * boolean and is still honoured when no basis is supplied.
 */
export function waiverBudgetSummary({
  usesBiddingBudget,
  budget,
  remaining,
  waiverPosition,
  budgetIsAssumed = false,
  budgetBasis = null,
} = {}) {
  // RULE 1, checked first. A league that does not bid gets no dollar figure under
  // any circumstances, including when the platform still reports a budget for it.
  if (usesBiddingBudget === false) {
    return {
      kind: 'no_bidding',
      text: isNum(waiverPosition) ? `waiver priority #${waiverPosition}` : null,
      isAssumption: false,
    }
  }

  // Still rule 1: without a definite "this league bids" we say nothing about money,
  // no matter what numbers or assumptions arrived alongside. A waiver order position
  // is withheld too — it is itself a claim about how the league works.
  if (usesBiddingBudget !== true) {
    return { kind: 'unknown', text: null, isAssumption: false }
  }

  // The league bids. Only the amounts are in question from here.
  if (budgetBasis === 'full_budget') {
    return {
      kind: 'spend_unknown',
      text: isNum(budget) ? `$${budget} budget, spend unknown` : null,
      isAssumption: true,
    }
  }
  if (budgetBasis === 'standard_budget' || (budgetBasis === null && budgetIsAssumed)) {
    const amount = isNum(remaining) ? remaining : (isNum(budget) ? budget : 100)
    return { kind: 'assumed', text: `assuming a $${amount} budget`, isAssumption: true }
  }

  if (isNum(budget) && isNum(remaining)) {
    return {
      kind: 'known',
      text: `$${remaining} of $${budget} budget left`,
      isAssumption: false,
    }
  }
  if (isNum(budget)) {
    return { kind: 'spend_unknown', text: `$${budget} budget`, isAssumption: false }
  }
  return { kind: 'budget_unknown', text: 'budget unavailable', isAssumption: false }
}
