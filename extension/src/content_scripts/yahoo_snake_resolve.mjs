/**
 * Yahoo SNAKE draft resolver — React client (2026 replatform).
 *
 * Yahoo migrated the snake room onto the SAME React root as auction
 * (`#main-0-DraftClientBootstrap-Proxy`), so the old `#app`-innerText + pick-card
 * poller is dead. This reads the React DOM structurally and is non-destructive —
 * NO "Picks"-tab click (that tab-mutation was the old cross-poller hazard). The
 * whole live state lives in the Board view:
 *   - the turn banner: "Your Turn • Round R, Pick P" OR
 *     "{Name}'s Pick • You're up in N Picks • Round R, Pick P"
 *   - the "Last:" indicator: the most recent pick (player + POS·Team)
 *   - the 12 team headers (draft order) + the serpentine board grid
 *
 * Snake and auction share the root, so the cross-poller guard is CONTENT-based:
 * snake activates only on the turn banner above (auction has "N nominations
 * until your turn", never "Round N, Pick N"); auction activates only on a
 * Proj-$ nominee or $-budget .ys-team cards (snake's .ys-team are budget-less).
 *
 * DOM-structural but free of network side effects → unit-testable by parsing
 * captured Yahoo outerHTML with linkedom (test/fixtures/auction/snake-*.html).
 */

export const SNAKE_ROOT_SELECTOR = '#main-0-DraftClientBootstrap-Proxy'
export const EXPECTED_YOU_LABEL = 'You'

const YOUR_TURN_RE = /Your Turn\s*[•·]\s*Round\s+(\d+),\s*Pick\s+(\d+)/i
const OTHER_PICK_RE = /^(.+?)'s Pick\b[\s\S]*?Round\s+(\d+),\s*Pick\s+(\d+)/i
const UP_IN_RE = /You're up in\s+(\d+)\s+Pick/i
const POS_TEAM_RE = /\(([A-Za-z]+)\s*[·•]\s*([A-Za-z]+)\)/

const txt = (el) => (el && el.textContent ? el.textContent.trim() : '')
const spanTexts = (root) =>
  root ? Array.from(root.querySelectorAll('span')).map(txt) : []

export function snakeRoot(doc) {
  return doc ? doc.querySelector(SNAKE_ROOT_SELECTOR) : null
}

/**
 * Parse the snake turn banner. A catch-all container span can hold the whole
 * app text, so only consider reasonably short spans (the dedicated banner is
 * ~50 chars) — avoids a concatenated mis-parse. Returns null on a non-snake page.
 */
export function resolveSnakeBanner(root) {
  const texts = spanTexts(root).filter((t) => t && t.length < 90)
  const yt = texts.find((t) => YOUR_TURN_RE.test(t))
  if (yt) {
    const m = yt.match(YOUR_TURN_RE)
    return {
      isYourTurn: true,
      round: parseInt(m[1], 10),
      pick: parseInt(m[2], 10),
      picker: EXPECTED_YOU_LABEL,
      picksUntilYourTurn: 0,
    }
  }
  const op = texts.find((t) => OTHER_PICK_RE.test(t))
  if (op) {
    const m = op.match(OTHER_PICK_RE)
    const up = op.match(UP_IN_RE)
    return {
      isYourTurn: false,
      round: parseInt(m[2], 10),
      pick: parseInt(m[3], 10),
      picker: m[1].trim(),
      picksUntilYourTurn: up ? parseInt(up[1], 10) : null,
    }
  }
  return null
}

/** True iff the page shows the snake turn banner (the cross-poller discriminator). */
export function hasSnakeContent(root) {
  return resolveSnakeBanner(root) != null
}

/** The snake poller activates ONLY on confirmed snake content within the root. */
export function shouldSnakeActivate(root) {
  return !!root && hasSnakeContent(root)
}

/**
 * Round-1 draft order = the first N `.ys-team` header cells in DOM order, where
 * N = the distinct team data-ids. The user's own cell reads "You".
 */
export function resolveTeamOrder(root) {
  const cells = root ? Array.from(root.querySelectorAll('.ys-team')) : []
  const n = new Set(cells.map((c) => c.getAttribute('data-id')).filter(Boolean)).size
  return cells.slice(0, n).map(txt)
}

/**
 * SERPENTINE slot for a pick number (0-indexed into the round-1 draft order).
 * Odd rounds run forward; even rounds reverse — so in a 12-team league pick 12
 * and pick 13 are the SAME team (last of round 1 picks again first in round 2).
 */
export function pickSlotIndex(pickNumber, teamCount) {
  if (!teamCount) return -1
  const round = Math.ceil(pickNumber / teamCount)
  const pos = (pickNumber - 1) % teamCount
  return round % 2 === 1 ? pos : teamCount - 1 - pos
}

/**
 * The most recent pick, from the "Last:" indicator + the serpentine board.
 * The CURRENT pick is on the clock; the last completed pick is currentPick − 1.
 * Player name is abbreviated ("C. Lamb") — the backend resolves it to full +
 * canonical id (same as it always has for snake).
 */
export function resolveLastPick(root, banner, teamOrder) {
  if (!banner || !banner.pick || banner.pick <= 1) return null
  const texts = spanTexts(root)
  const i = texts.findIndex((t) => t === 'Last:')
  const player_name = i >= 0 ? texts[i + 1] || null : null
  if (!player_name) return null
  const pt = (texts[i + 2] || '').match(POS_TEAM_RE)
  const teamCount = teamOrder.length || 0
  const pickNumber = banner.pick - 1
  const slot = pickSlotIndex(pickNumber, teamCount)
  const picker = slot >= 0 ? teamOrder[slot] ?? null : null
  return {
    pick_number: pickNumber,
    player_name,
    position: pt ? pt[1] : null,
    nfl_team: pt ? pt[2] : null,
    picker,
    is_yours: picker === EXPECTED_YOU_LABEL,
    round: teamCount ? Math.ceil(pickNumber / teamCount) : null,
  }
}

/** Resolve the full snake board state from the React root. */
export function resolveSnakeState(root) {
  const banner = resolveSnakeBanner(root)
  const teamOrder = resolveTeamOrder(root)
  const lastPick = resolveLastPick(root, banner, teamOrder)
  return {
    isYourTurn: !!(banner && banner.isYourTurn),
    yourRound: banner && banner.isYourTurn ? banner.round : null,
    yourPick: banner && banner.isYourTurn ? banner.pick : null,
    currentRound: banner ? banner.round : null,
    currentPick: banner ? banner.pick : null,
    currentPicker: banner ? banner.picker : null,
    picksUntilYourTurn: banner ? banner.picksUntilYourTurn : null,
    lastPick,
    teamCount: teamOrder.length,
  }
}

// ---------------------------------------------------------------------------
// Event diffing — emit the SAME events the backend/frontend already handle:
// your_turn, your_turn_soon, snake_status, snake_pick. ON CHANGE only.
// ---------------------------------------------------------------------------
export function initSnakeMemory() {
  return {
    wasYourTurn: false,
    lastPicksUntil: null,
    lastStatus: null,
    sentPickNumbers: [], // dedupe snake_pick per pick number
  }
}

export function detectSnakeEvents(prev, curr) {
  const events = []
  const next = { ...prev, sentPickNumbers: prev.sentPickNumbers.slice() }

  // YOUR TURN — rising edge of the on-the-clock state.
  if (curr.isYourTurn && !prev.wasYourTurn) {
    events.push({
      type: 'your_turn',
      platform: 'yahoo',
      payload: { round: curr.yourRound, pick: curr.yourPick, picks_until_your_turn: 0 },
    })
  }

  // PICK COMING SOON — fire once when exactly 2 picks away.
  if (!curr.isYourTurn && curr.picksUntilYourTurn === 2 && prev.lastPicksUntil !== 2) {
    events.push({
      type: 'your_turn_soon',
      platform: 'yahoo',
      payload: { picks_until_your_turn: 2, round: curr.currentRound },
    })
  }

  // CONTINUOUS STATUS — current pick/round + countdown, on change.
  const status = {
    current_pick: curr.currentPick,
    current_round: curr.currentRound,
    picks_until_your_turn: curr.picksUntilYourTurn,
  }
  const ps = prev.lastStatus || {}
  if (
    status.current_pick !== ps.current_pick ||
    status.current_round !== ps.current_round ||
    status.picks_until_your_turn !== ps.picks_until_your_turn
  ) {
    events.push({ type: 'snake_status', platform: 'yahoo', payload: status })
  }

  // SNAKE PICK — the just-completed pick (from "Last:"), deduped by pick number.
  if (curr.lastPick && !next.sentPickNumbers.includes(curr.lastPick.pick_number)) {
    const lp = curr.lastPick
    events.push({
      type: 'snake_pick',
      platform: 'yahoo',
      payload: {
        pick_number: lp.pick_number,
        player_name: lp.player_name,
        position: lp.position,
        nfl_team: lp.nfl_team,
        picker: lp.picker,
        is_yours: lp.is_yours,
        round: lp.round,
      },
    })
    next.sentPickNumbers.push(lp.pick_number)
  }

  next.wasYourTurn = curr.isYourTurn
  next.lastPicksUntil = curr.picksUntilYourTurn
  next.lastStatus = status
  return { events, next }
}
