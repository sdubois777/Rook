/**
 * Pure parsing + event-detection helpers for the Yahoo draft room poller.
 *
 * Kept free of any browser/DOM/network side effects so the logic is
 * unit-testable in plain Node (see extension/test/yahoo_draft.test.mjs).
 * The content script (yahoo_draft.js) supplies the live `#draft` innerText
 * and relays the detected events; everything here is a pure function.
 */

/**
 * Parse a single team budget line, e.g. "Stephen$96/15".
 *
 * Format is {name}${budget}{slotsUsed}/{totalSlots} with the budget/slots
 * split ambiguous ("96" → budget 9, slots 6). Disambiguate by trying a
 * 1- then 2-digit slots suffix and accepting the first split that yields
 * a plausible (0..totalSlots) slot count and (0..200) budget.
 */
export function parseTeamLine(line) {
  const match = line.match(/^(.+?)\$(\d+)\/(\d+)$/)
  if (!match) return null

  const name = match[1]
  const numStr = match[2]
  const totalSlots = parseInt(match[3], 10)

  for (const digits of [1, 2]) {
    if (numStr.length <= digits) continue
    const slotsUsed = parseInt(numStr.slice(-digits), 10)
    const budget = parseInt(numStr.slice(0, -digits), 10)
    if (
      slotsUsed >= 0 &&
      slotsUsed <= totalSlots &&
      budget >= 0 &&
      budget <= 200
    ) {
      return { name, budget, slotsUsed, totalSlots }
    }
  }
  return null
}

// NOTE (2026 React replatform): the auction activation gate moved to
// yahoo_auction_resolve.mjs (`shouldAuctionActivate(doc, …)` — root + live
// signal + cross-poller veto). The old `#draft`-presence gate that lived here is
// gone; Yahoo removed the `#draft` node. parseDraftState/detectEvents below are
// LEGACY innerText helpers (no longer wired to a content script); detectWinner
// and secondsFromClock are still reused by the React resolver.

/** Total seconds remaining from a "M:SS" clock string ("0:19" → 19). */
export function secondsFromClock(clock) {
  if (!clock) return null
  const parts = clock.split(':')
  if (parts.length !== 2) return null
  const mins = parseInt(parts[0], 10)
  const secs = parseInt(parts[1], 10)
  if (Number.isNaN(mins) || Number.isNaN(secs)) return null
  return mins * 60 + secs
}

/**
 * Parse the draft room state out of `#draft` innerText.
 *
 * Returns { playerName, posTeam, currentBid, clock, teams } or null when
 * the text is empty. Between nominations playerName is null (waiting state).
 */
export function parseDraftState(text) {
  if (!text) return null

  const lines = text
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0)

  // Player on the block sits on the line before the "XXX – YY" pos/team line
  const posIdx = lines.findIndex((l) =>
    /^[A-Z]{2,3}\s[–-]\s[A-Z]{1,3}$/.test(l)
  )
  const playerName = posIdx > 0 ? lines[posIdx - 1] : null
  const posTeam = posIdx >= 0 ? lines[posIdx] : null

  // Current bid — "$XX"
  const bidLine = lines.find((l) => /^\$\d+$/.test(l))
  const currentBid = bidLine ? parseInt(bidLine.replace('$', ''), 10) : null

  // Clock — "X:XX Remaining"
  const clockLine = lines.find((l) => l.includes('Remaining'))
  const clock = clockLine ? clockLine.replace(' Remaining', '').trim() : null

  // Team budgets — "{name}${budget}{slots}/{total}"
  const teams = {}
  lines
    .filter((l) => /^.+\$\d+\/\d+$/.test(l))
    .forEach((line) => {
      const parsed = parseTeamLine(line)
      if (parsed) {
        teams[parsed.name] = {
          budget: parsed.budget,
          slotsUsed: parsed.slotsUsed,
          totalSlots: parsed.totalSlots,
        }
      }
    })

  return { playerName, posTeam, currentBid, clock, teams }
}

/**
 * Find which team won the just-sold player by budget/slot delta:
 * the winner's slotsUsed went up by one and budget dropped by the price.
 */
export function detectWinner(current, prev, price) {
  for (const [name, curr] of Object.entries(current)) {
    const p = prev[name]
    if (!p) continue
    if (curr.slotsUsed === p.slotsUsed + 1 && curr.budget === p.budget - price) {
      return name
    }
  }
  return null
}

/**
 * Diff the previous tracked memory against the freshly parsed state and
 * return the draft events to relay plus the updated memory.
 *
 * `prev` carries { lastPlayer, lastBid, lastClock, prevTeams }.
 * Pure: callers thread `next` back in on the following tick. This is the
 * unit-testable core of the 300ms poller loop.
 */
export function detectEvents(prev, curr) {
  const events = []
  const next = { ...prev }
  const { playerName, posTeam, currentBid, clock, teams } = curr

  // NOMINATION — a new player appeared on the block
  if (playerName && playerName !== prev.lastPlayer) {
    events.push({
      type: 'nomination',
      platform: 'yahoo',
      payload: {
        player_name: playerName,
        pos_team: posTeam,
        opening_bid: currentBid,
        clock,
      },
    })
    next.lastPlayer = playerName
    next.lastBid = currentBid
    next.lastClock = clock
    next.prevTeams = { ...teams }
  }

  // BID UPDATE — same player, bid changed
  if (playerName && currentBid !== next.lastBid) {
    events.push({
      type: 'bid_update',
      platform: 'yahoo',
      payload: { player_name: playerName, current_bid: currentBid, clock },
    })
    next.lastBid = currentBid
  }

  // CLOCK — emit on a 5-second cadence to keep the UI ticking
  if (playerName && clock !== next.lastClock) {
    next.lastClock = clock
    const secs = secondsFromClock(clock)
    if (secs !== null && secs % 5 === 0) {
      events.push({
        type: 'clock',
        platform: 'yahoo',
        payload: { player_name: playerName, clock, seconds_remaining: secs },
      })
    }
  }

  // SOLD — player went null after being on the block
  if (!playerName && prev.lastPlayer) {
    const winner = detectWinner(teams, prev.prevTeams || {}, prev.lastBid)
    events.push({
      type: 'draft_pick',
      platform: 'yahoo',
      payload: {
        player_name: prev.lastPlayer,
        final_price: prev.lastBid,
        winner: winner || 'unknown',
        teams_snapshot: teams,
      },
    })
    next.prevTeams = { ...teams }
    next.lastPlayer = null
    next.lastBid = null
    next.lastClock = null
  }

  // TEAMS UPDATE — budgets shifted with no active nomination
  if (
    !playerName &&
    JSON.stringify(teams) !== JSON.stringify(next.prevTeams)
  ) {
    events.push({
      type: 'teams_update',
      platform: 'yahoo',
      payload: { teams },
    })
    next.prevTeams = { ...teams }
  }

  return { events, next }
}
