/**
 * Yahoo AUCTION draft resolver — React-client DOM (2026 replatform).
 *
 * Yahoo rebuilt the auction room as a React app rooted at
 * `#main-0-DraftClientBootstrap-Proxy` with NO semantic ids/classes on live
 * data. Two class families:
 *   - `ys-*` KEBAB classes (e.g. `.ys-team`, `.ys-player`) are hand-authored
 *     and semantic — OK as structural anchors.
 *   - `_ys_*` HASH classes are build-generated and ROTATE every Yahoo deploy —
 *     NEVER a primary key; only a fallback layered behind a text/structure check,
 *     and using one fires LOUD degradation telemetry (console.warn +
 *     selector_health) so the next rotation alarms instead of silently stalling.
 *
 * This module is DOM-structural (takes an Element root) but free of network
 * side effects, so it is unit-testable by parsing captured Yahoo outerHTML with
 * linkedom (see test/fixtures/auction/). Pure event-diffing helpers
 * (detectWinner, secondsFromClock) are reused from yahoo_draft_parse.mjs.
 *
 * STATUS: scaffold. The gate + .ys-team team parsing are verifiable against the
 * lobby capture. The nomination/name/bid/clock/bidder field tuning is HELD until
 * a real mid-nomination capture lands — those resolvers are wired to the agreed
 * strategy but must NOT be trusted/tuned against a single snapshot.
 */
import { detectWinner, secondsFromClock } from './yahoo_draft_parse.mjs'

export const AUCTION_ROOT_SELECTOR = '#main-0-DraftClientBootstrap-Proxy'

// Locale-configurable expected label for the user's own team card (Amendment A).
export const EXPECTED_YOU_LABEL = 'You'

// How far up from the clock span the nomination card may be (Amendment 1: cap
// the ascent so the LCA can't balloon toward the whole board).
const CARD_MAX_ASCENT = 6

const MONEY_RE = /^\$\d+/
const CLOCK_RE = /^\d{2}:\d{2}$/
const ROSTER_RE = /^\d+\/\d+$/
const TURN_RE = /(\d+)\s+nominations?\s+until your turn/i
// "your turn now" wording — locked to the real capture: "It's your turn to
// nominate" (your-turn.html). Match the stable core phrase.
const YOUR_TURN_NOW_RE = /your turn to nominate/i

// Known non-name labels that must never be read as a player name.
const KNOWN_LABELS = new Set([
  'You', 'Current Bid', 'Sold', 'Nominate', 'Bid', 'Pass', 'Queue',
  'Add to Queue', 'Your Turn', 'Board', 'Players', 'Results', 'Standings',
])

// ---------------------------------------------------------------------------
// Small DOM helpers (kept tiny so linkedom covers them)
// ---------------------------------------------------------------------------
const txt = (el) => (el && el.textContent ? el.textContent.trim() : '')
const spansIn = (el) => (el ? Array.from(el.querySelectorAll('span')) : [])
const notInDialog = (el) => !!el && !el.closest('[role="dialog"]')

export function auctionRoot(doc) {
  return doc ? doc.querySelector(AUCTION_ROOT_SELECTOR) : null
}

/** The live countdown span: MM:SS text, NOT inside a dialog/modal subtree. */
export function findLiveTimer(root) {
  if (!root) return null
  return spansIn(root).find((s) => CLOCK_RE.test(txt(s)) && notInDialog(s)) || null
}

/**
 * Negative gate: a post-draft summary/results screen shares the React root.
 * Verified from draft-complete.html: that screen has NO .ys-team cards and no
 * live timer, so the live-signal gate already keeps it inactive. This marker is
 * defense-in-depth (in case a future summary keeps the team cards) — anchored on
 * the draft-complete greeting "Thank you for drafting with Yahoo".
 */
export function isDraftComplete(root) {
  if (!root) return false
  return Array.from(root.querySelectorAll('span')).some((s) =>
    /thank you for drafting/i.test(txt(s))
  )
}

// ---------------------------------------------------------------------------
// Activation gate (pure decision + DOM wrapper)
// ---------------------------------------------------------------------------

/**
 * `.ys-team` cards carrying a $-budget span. This is the AUCTION discriminator:
 * auction team cards always show a $ budget (12/12 in every captured state),
 * whereas snake `.ys-team` cards (180 of them — the serpentine board grid) are
 * budget-LESS. Counting only budgeted cards is what keeps the auction poller from
 * false-activating on a snake page that happens to share the same React root.
 */
export function budgetTeamCount(root) {
  if (!root) return 0
  return Array.from(root.querySelectorAll('.ys-team')).filter((card) =>
    spansIn(card).some((s) => MONEY_RE.test(txt(s)))
  ).length
}

/**
 * Pure gate decision (unit-testable without a DOM). Active iff the React root is
 * present, the draft isn't complete, and there's POSITIVE AUCTION content — a
 * Proj-$ nominee OR at least one $-budget team card. Deliberately content-only:
 * - NOT the live timer — snake also has a 00:xx pick clock, so a timer arm would
 *   false-trip on snake.
 * - NOT bare `.ys-team` — snake's 180 budget-less board cells would false-trip it.
 * - NO snake-marker veto / `#app` discriminator — the guard gates purely on its
 *   own positive signal, so a shared root is harmless either way.
 */
export function auctionGateDecision({ hasRoot, hasNominee, budgetTeamCount, draftComplete }) {
  if (!hasRoot) return false
  if (draftComplete) return false
  return !!hasNominee || (budgetTeamCount || 0) >= 1
}

/** DOM wrapper: compute the gate inputs from a document/root and decide. */
export function shouldAuctionActivate(doc) {
  const root = auctionRoot(doc)
  if (!root) return false
  return auctionGateDecision({
    hasRoot: true,
    hasNominee: !!findNomineeEl(root),
    budgetTeamCount: budgetTeamCount(root),
    draftComplete: isDraftComplete(root),
  })
}

// ---------------------------------------------------------------------------
// Selector health (loud degradation telemetry)
// ---------------------------------------------------------------------------
function freshHealth() {
  // 'na' = not applicable this tick (e.g. no active nomination → no clock yet),
  // 'primary' = resolved off a stable anchor, 'fallback' = off a _ys_ hash,
  // 'missing' = anchor present but nothing matched.
  return { clock: 'na', name: 'na', bid: 'na', bidder: 'na', teams: 'na', turn: 'na' }
}

// ---------------------------------------------------------------------------
// Field resolvers (strategy wired; field tuning pending live nomination capture)
// ---------------------------------------------------------------------------
export function isNameShape(text) {
  const t = (text || '').trim()
  if (!t || t.length > 40) return false
  if (MONEY_RE.test(t) || CLOCK_RE.test(t) || ROSTER_RE.test(t)) return false
  if (/^\d/.test(t)) return false
  if (KNOWN_LABELS.has(t)) return false
  if (!/[A-Z]/.test(t)) return false
  return t.split(/\s+/).length >= 2 // >=2 tokens (Amendment 1)
}

/** Teams + your-team self-id. Verifiable against the lobby capture (Amendment A). */
export function resolveTeams(root, health, warn) {
  const cards = root ? Array.from(root.querySelectorAll('.ys-team')) : []
  if (cards.length === 0) {
    health.teams = 'missing'
    return { teams: {}, yourTeamId: null }
  }
  const teams = {}
  let yourTeamId = null
  let sawName = true
  for (const card of cards) {
    const dataId = card.getAttribute('data-id')
    const spans = spansIn(card)
    const budgetSpan = spans.find((s) => MONEY_RE.test(txt(s)))
    const rosterSpan = spans.find((s) => ROSTER_RE.test(txt(s)))
    const isYou = spans.some((s) => txt(s) === EXPECTED_YOU_LABEL)
    const nameSpan = spans.find((s) => {
      const t = txt(s)
      return t && t !== EXPECTED_YOU_LABEL && !MONEY_RE.test(t) && !ROSTER_RE.test(t)
    })
    // The user's own card shows "You", not a team name — that's expected, so
    // key it by "You" and DON'T treat its missing team-name span as a
    // degradation (only an OPPONENT missing its name is a real miss).
    const name = nameSpan
      ? txt(nameSpan)
      : isYou
      ? EXPECTED_YOU_LABEL
      : dataId
      ? `Team ${dataId}`
      : null
    if (!nameSpan && !isYou) sawName = false
    const budget = budgetSpan ? parseInt(txt(budgetSpan).replace(/[^\d]/g, ''), 10) : null
    let slotsUsed = null
    let totalSlots = null
    const rm = rosterSpan ? txt(rosterSpan).match(ROSTER_RE) : null
    if (rm) {
      const parts = txt(rosterSpan).split('/')
      slotsUsed = parseInt(parts[0], 10)
      totalSlots = parseInt(parts[1], 10)
    }
    if (name != null) {
      teams[name] = { budget, slotsUsed, totalSlots, dataId } // data-id = stable key
    }
    if (isYou && dataId != null) yourTeamId = dataId // PRIMARY self-id (Amendment A)
  }
  // Self-id fallback: _ys_1659jmf behind the You/data-id checks (loud).
  if (yourTeamId == null) {
    const fb = root.querySelector('.ys-team span._ys_1659jmf')
    const card = fb ? fb.closest('.ys-team') : null
    if (card) {
      yourTeamId = card.getAttribute('data-id')
      warn('team_self_id', 'fallback')
    }
  }
  health.teams = sawName ? 'primary' : 'fallback'
  return { teams, yourTeamId }
}

/**
 * The nominee on the block: the single ys-player[data-id] in the offer panel,
 * distinguished from the (many) Players-table rows by its SHORT text carrying
 * the projected price ("Proj $N") — the table rows are long stat lines. Returns
 * null when no nomination is active (e.g. an empty lobby) so the resolver
 * reports no nominee instead of grabbing the first player in the table.
 */
export function findNomineeEl(root) {
  const yps = root
    ? Array.from(root.querySelectorAll('[class~="ys-player"][data-id], .ys-player[data-id]'))
    : []
  return (
    yps.find((p) => {
      const t = txt(p)
      return t.length < 60 && /Proj/i.test(t) && /\$/.test(t)
    }) || null
  )
}

/**
 * The nomination "offer panel" = the nominee's LARGEST ancestor that still
 * excludes the team-budget SIDEBAR (no `.ys-team`) and the Players TABLE (≤1
 * ys-player). That structural boundary is what keeps the bidder resolution off
 * the sidebar — anchoring on the timer jumped straight to the everything-
 * container (12 .ys-team), which is how the bidder mis-resolved to a sidebar
 * team. Structural, not positional.
 */
export function resolveNominationCard(root) {
  const nominee = findNomineeEl(root)
  if (!nominee) return null
  let card = nominee
  let depth = 0
  while (card.parentElement && depth < CARD_MAX_ASCENT) {
    const p = card.parentElement
    if (p.querySelectorAll('.ys-team').length > 0) break
    if (p.querySelectorAll('[class~="ys-player"], .ys-player').length > 1) break
    card = p
    depth += 1
  }
  return card
}

/** Player ID (Amendment B PRIMARY) — stable across deploys. From the nominee. */
export function resolvePlayerId(nominee) {
  return nominee ? nominee.getAttribute('data-id') : null
}

const POS_RE = /^(QB|RB|WR|TE|K|DEF|DST)$/

/** Best-effort "POS · Team" from the nominee element (backend resolves by name). */
export function resolvePosTeam(nominee) {
  if (!nominee) return null
  const spans = spansIn(nominee).map(txt)
  const i = spans.findIndex((t) => POS_RE.test(t))
  if (i < 0) return null
  const team = spans[i + 1] && /^[A-Za-z]{2,3}$/.test(spans[i + 1]) ? spans[i + 1] : null
  return team ? `${spans[i]} · ${team}` : spans[i]
}

/**
 * Player name. Primary = the ys-player[data-id] row's name (Amendment B);
 * fallback = name-shape span in document order; last resort = the _ys_ name
 * span. Sets health.name and warns on any fallback.
 */
export function resolvePlayerName(nominee, card, health, warn) {
  if (!nominee && !card) {
    health.name = 'na'
    return null
  }
  // Primary: name span inside the id-anchored nominee element (Amendment B).
  const idName = nominee ? spansIn(nominee).map(txt).find(isNameShape) : null
  if (idName) {
    health.name = 'primary'
    return idName
  }
  // Fallback: first name-shape span in the offer panel (document order).
  const shapeName = card ? spansIn(card).map(txt).find(isNameShape) : null
  if (shapeName) {
    health.name = 'fallback'
    warn('name', 'shape')
    return shapeName
  }
  // Last resort: the rotating _ys_ name span, behind the shape gate.
  const fb = card ? card.querySelector('span._ys_1i9qkex') : null
  if (fb && isNameShape(txt(fb))) {
    health.name = 'fallback'
    warn('name', 'hash')
    return txt(fb)
  }
  health.name = 'missing'
  if (card) warn('name', 'missing')
  return null
}

/** Current bid: the money span INSIDE the nomination card (not a .ys-team budget). */
export function resolveBid(card, health, warn) {
  if (!card) {
    health.bid = 'na'
    return null
  }
  const moneySpan = spansIn(card).find(
    (s) => MONEY_RE.test(txt(s)) && !s.closest('.ys-team')
  )
  if (moneySpan) {
    health.bid = 'primary'
    return parseInt(txt(moneySpan).replace(/[^\d]/g, ''), 10)
  }
  const fb = card.querySelector('span._ys_uurq5p')
  if (fb && MONEY_RE.test(txt(fb))) {
    health.bid = 'fallback'
    warn('bid', 'hash')
    return parseInt(txt(fb).replace(/[^\d]/g, ''), 10)
  }
  // No bid in the panel. NOT necessarily a degradation — the "your turn to
  // nominate" state shows a suggested player with no bid yet. The caller decides
  // (active-vs-suggested) and resets health to 'na' when there's no live bid.
  health.bid = 'missing'
  return null
}

/**
 * Current high-bidder team (Amendment 5). Resilient: the text in the card that
 * MATCHES a known .ys-team name (cross-checked). _ys_aug67i is the fallback.
 */
export function resolveBidder(card, teams, health, warn) {
  if (!card) {
    health.bidder = 'na'
    return { name: null, teamId: null }
  }
  // Structural: a team label WITHIN the offer panel. The card excludes the
  // .ys-team sidebar, so a known-team-name match here is the high bidder, not a
  // stray sidebar card. Cross-check to the stable .ys-team[data-id] so we thread
  // a team ID, not just the display string (Amendment 5) — NOT positional.
  const idByName = new Map(
    Object.entries(teams || {}).map(([n, v]) => [n, v.dataId ?? null])
  )
  const hit = spansIn(card)
    .map(txt)
    .find((t) => idByName.has(t))
  if (hit) {
    health.bidder = 'primary'
    return { name: hit, teamId: idByName.get(hit) }
  }
  const fb = card.querySelector('span._ys_aug67i')
  if (fb && idByName.has(txt(fb))) {
    health.bidder = 'fallback'
    warn('bidder', 'hash')
    return { name: txt(fb), teamId: idByName.get(txt(fb)) }
  }
  health.bidder = teams && Object.keys(teams).length ? 'missing' : 'na'
  return { name: null, teamId: null } // best-effort; absence is not fatal
}

/** "N nominations until your turn" → viewer countdown (heartbeat-only data). */
export function resolveTurn(root, health) {
  const texts = spansIn(root).map(txt)
  // Prefer a span whose ENTIRE text is the countdown. A catch-all container
  // span can concatenate the whole app text (e.g. the clock "00:19" abutting
  // "4 nominations until your turn" → a false "194"), so a full-match anchor is
  // required; only fall back to a substring match (loud) if no clean span.
  const FULL = /^(\d+)\s+nominations?\s+until your turn$/i
  for (const t of texts) {
    const m = t.match(FULL)
    if (m) {
      health.turn = 'primary'
      return parseInt(m[1], 10)
    }
  }
  if (texts.some((t) => t.length < 60 && YOUR_TURN_NOW_RE.test(t))) {
    health.turn = 'primary'
    return 0 // your turn now
  }
  for (const t of texts) {
    const m = t.match(TURN_RE)
    if (m) {
      health.turn = 'fallback' // matched inside a noisy/blob span
      return parseInt(m[1], 10)
    }
  }
  health.turn = 'na'
  return null
}

// ---------------------------------------------------------------------------
// Top-level resolver
// ---------------------------------------------------------------------------
/**
 * Resolve the full auction board state from the React root. `warn(field, level)`
 * is the loud-degradation hook (the content script throttles + feeds telemetry).
 */
export function resolveAuctionState(root, { warn = () => {} } = {}) {
  const health = freshHealth()
  const { teams, yourTeamId } = resolveTeams(root, health, warn)
  const card = resolveNominationCard(root)
  const nominee = findNomineeEl(root)
  const currentBid = resolveBid(card, health, warn)

  // An ACTIVE nomination requires a live bid. "It's your turn to nominate" shows
  // a SUGGESTED player (Proj $, no bid) and a lobby shows none — neither is a
  // nomination, so they must not emit one or report degraded name/clock/bidder.
  const active = card != null && currentBid != null
  let playerName = null
  let playerId = null
  let posTeam = null
  let currentBidder = null
  let currentBidderTeamId = null
  let clock = null
  if (active) {
    playerId = resolvePlayerId(nominee)
    playerName = resolvePlayerName(nominee, card, health, warn)
    posTeam = resolvePosTeam(nominee)
    const bidder = resolveBidder(card, teams, health, warn)
    currentBidder = bidder.name
    currentBidderTeamId = bidder.teamId
    clock = resolveClock(root, health, warn)
  } else {
    health.name = 'na'
    health.bid = 'na'
    health.bidder = 'na'
    health.clock = 'na'
  }
  const picksUntilYourTurn = resolveTurn(root, health)
  return {
    playerName,
    playerId,
    posTeam,
    currentBid: active ? currentBid : null,
    currentBidder,
    currentBidderTeamId,
    clock,
    teams,
    yourTeamId,
    picksUntilYourTurn,
    health,
  }
}

/** The live countdown (root-level widget) → _ys_ fallback behind the text check. */
export function resolveClock(root, health, warn) {
  const prim = findLiveTimer(root)
  if (prim) {
    health.clock = 'primary'
    return txt(prim)
  }
  const fb = root ? root.querySelector('span._ys_12k0qlu') : null
  if (fb && CLOCK_RE.test(txt(fb)) && notInDialog(fb)) {
    health.clock = 'fallback'
    warn('clock', 'hash')
    return txt(fb)
  }
  health.clock = 'missing'
  warn('clock', 'missing')
  return null
}

// ---------------------------------------------------------------------------
// Event diffing — emit ON CHANGE; nomination 1-tick debounced; draft_pick is
// team-delta driven (Amendment 4); bid_update carries current_bidder
// (Amendment 5); heartbeat carries selector_health + countdown (Answer 3).
// ---------------------------------------------------------------------------
export function initAuctionMemory() {
  return {
    lastPlayerKey: null,
    pendingPlayerKey: null,
    lastPlayerName: null,
    lastPlayerId: null,
    lastBid: null,
    lastBidder: null,
    lastClock: null,
    nominationTeams: {}, // snapshot at nomination time → baseline for sale delta
    prevTeams: {}, // last seen teams → teams_update change detection
    soldKeys: [], // dedupe draft_pick per player key
    lastHealth: null,
    lastPicksUntil: null,
  }
}

export function detectAuctionEvents(prev, curr) {
  const events = []
  const next = { ...prev, soldKeys: prev.soldKeys.slice() }
  const key = curr.playerName ? curr.playerId || curr.playerName : null // nominee identity

  // SALE — the previously-confirmed nominee is no longer the active nominee
  // (sold, or replaced by the next nomination). Every nominated player IS sold
  // in an auction (min $1), so this MUST fire even when the winner can't be
  // determined from the budget delta ('unknown') — otherwise the player never
  // leaves the board. Attribute the winner via the team-budget delta; tag
  // is_yours so the UI books it to your team, not a phantom "You".
  if (prev.lastPlayerKey && key !== prev.lastPlayerKey && !next.soldKeys.includes(prev.lastPlayerKey)) {
    const winner = detectWinner(curr.teams, prev.nominationTeams || {}, prev.lastBid) || 'unknown'
    events.push({
      type: 'draft_pick',
      platform: 'yahoo',
      payload: {
        player_name: prev.lastPlayerName,
        player_id: prev.lastPlayerId,
        final_price: prev.lastBid,
        winner,
        is_yours: winner === EXPECTED_YOU_LABEL,
        teams_snapshot: curr.teams,
      },
    })
    next.soldKeys.push(prev.lastPlayerKey)
    next.lastPlayerKey = null // cleared so the next nomination can stage
    next.lastPlayerName = null
    next.lastPlayerId = null
    next.pendingPlayerKey = null
  }

  // NOMINATION (1-tick confirmation debounce — Amendment 1).
  if (curr.playerName && key !== next.lastPlayerKey) {
    if (key === prev.pendingPlayerKey) {
      events.push({
        type: 'nomination',
        platform: 'yahoo',
        payload: {
          player_name: curr.playerName,
          player_id: curr.playerId,
          pos_team: curr.posTeam,
          opening_bid: curr.currentBid,
          current_bidder: curr.currentBidder,
          current_bidder_team_id: curr.currentBidderTeamId,
          clock: curr.clock,
        },
      })
      next.lastPlayerKey = key
      next.lastPlayerName = curr.playerName
      next.lastPlayerId = curr.playerId
      next.lastBid = curr.currentBid
      next.lastBidder = curr.currentBidder
      next.lastClock = curr.clock
      next.nominationTeams = { ...curr.teams }
      next.pendingPlayerKey = null
    } else {
      next.pendingPlayerKey = key // stage; confirm next tick
    }
  } else if (curr.playerName && key === next.lastPlayerKey) {
    next.pendingPlayerKey = null
  }

  // BID UPDATE (same player; amount OR high-bidder changed — Amendment 5).
  if (
    curr.playerName &&
    key === next.lastPlayerKey &&
    (curr.currentBid !== next.lastBid || curr.currentBidder !== next.lastBidder)
  ) {
    events.push({
      type: 'bid_update',
      platform: 'yahoo',
      payload: {
        player_name: curr.playerName,
        current_bid: curr.currentBid,
        current_bidder: curr.currentBidder,
        current_bidder_team_id: curr.currentBidderTeamId,
        clock: curr.clock,
      },
    })
    next.lastBid = curr.currentBid
    next.lastBidder = curr.currentBidder
  }

  // CLOCK (same player; 5s cadence to keep the UI ticking without spam).
  if (curr.playerName && curr.clock !== next.lastClock) {
    next.lastClock = curr.clock
    const secs = secondsFromClock(curr.clock)
    if (secs !== null && secs % 5 === 0) {
      events.push({
        type: 'clock',
        platform: 'yahoo',
        payload: { player_name: curr.playerName, clock: curr.clock, seconds_remaining: secs },
      })
    }
  }

  // TEAMS UPDATE (budgets/rosters changed) — carries data-ids + your_team_id.
  if (JSON.stringify(curr.teams) !== JSON.stringify(prev.prevTeams)) {
    events.push({
      type: 'teams_update',
      platform: 'yahoo',
      payload: { teams: curr.teams, your_team_id: curr.yourTeamId },
    })
    next.prevTeams = { ...curr.teams }
  }

  // HEARTBEAT — selector_health + viewer countdown. Emitted on health OR
  // countdown change (NOT every tick). NOTE: confirm whether you want strictly
  // health-change-only; countdown-change is included so "N until your turn"
  // stays fresh as your turn nears (Answer 3).
  const healthStr = JSON.stringify(curr.health)
  if (healthStr !== prev.lastHealth || curr.picksUntilYourTurn !== prev.lastPicksUntil) {
    events.push({
      type: 'heartbeat',
      platform: 'yahoo',
      payload: {
        selector_health: curr.health,
        picks_until_your_turn: curr.picksUntilYourTurn,
      },
    })
    next.lastHealth = healthStr
    next.lastPicksUntil = curr.picksUntilYourTurn
  }

  return { events, next }
}
