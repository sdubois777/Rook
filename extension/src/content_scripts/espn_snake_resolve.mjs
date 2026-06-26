/**
 * ESPN SNAKE resolver → your_turn / your_turn_soon / snake_status / snake_pick.
 *
 * Anchors (all PRIMARY):
 *   - Status: `[data-testid="current-pick"]` → `.on-the-clock` ("On the Clock:
 *     Pick 11") + `.team-name`/`title` (on-clock team); `[data-testid="clock"]` →
 *     "RND r of R" + digits.
 *   - Picklist (upcoming): `.picklist .pick-component` → `.pick-number`
 *     ("PICK 12"), `.team-name`, `.auto-word`.
 *   - Board (full history, non-destructive): `.completedPick` with inline
 *     `grid-area: row / col`; `.roundPick` ("1.1"), name spans, `.playerProTeam`,
 *     `.positionPill`, `.byeWeek`. Column→team + self via the board header cells
 *     (`.draft-board-grid-header-cell` / `.myTeam` / `.onTheClock`).
 *
 * Self-team comes from the board's `.myTeam` header (URL teamId is a live cross-
 * check). The 4807-byte status-widget captures lack the board, so the resolver
 * accepts an optional `myTeam` override for those (runtime always has the board).
 *
 * Emits the existing snake event contract verbatim. linkedom-testable
 * (test/fixtures/espn/snake/).
 */
import {
  txt,
  num,
  resolveClock,
  resolveBoardHeaders,
  resolveMyTeam,
  resolveCompletedPicks,
} from './espn_shared.mjs'

/** Gate: snake is active on `current-pick` with NO auction pick train. */
export function isSnake(root) {
  if (!root) return false
  if (root.querySelector('[data-testid="auction-pick"]')) return false
  return !!root.querySelector('[data-testid="current-pick"]')
}

/** Upcoming picks from the picklist → [{ pickNum, team, auto }]. */
export function resolvePicklist(root) {
  const items = root ? Array.from(root.querySelectorAll('.picklist .pick-component')) : []
  return items
    .map((p) => ({
      pickNum: num(txt(p.querySelector('.pick-number'))),
      team: txt(p.querySelector('.team-name')) || null,
      auto: !!txt(p.querySelector('.auto-word')),
    }))
    .filter((p) => p.pickNum != null)
}

/** Full snake board state. `opts.myTeam` overrides board-derived self-id. */
export function resolveSnakeState(root, opts = {}) {
  const clock = resolveClock(root)
  const cp = root && root.querySelector('[data-testid="current-pick"]')
  const onClockText = cp ? txt(cp.querySelector('.on-the-clock')) : ''
  const onClockTeam =
    (cp && (cp.getAttribute('title') || txt(cp.querySelector('.team-name')))) || null
  const currentPick = num(onClockText) // "On the Clock: Pick 11" → 11
  const headers = resolveBoardHeaders(root)
  const myTeam = opts.myTeam || resolveMyTeam(root)
  const picklist = resolvePicklist(root)
  const completedPicks = resolveCompletedPicks(root, headers)
  const teamCount = headers.length || null

  const isYourTurn = !!(myTeam && onClockTeam && onClockTeam === myTeam)
  // picks until my next turn = my next picklist pick number − current pick number.
  let picksUntil = null
  if (!isYourTurn && myTeam && currentPick != null) {
    const mine = picklist.find((p) => p.team === myTeam)
    if (mine) picksUntil = mine.pickNum - currentPick
  } else if (isYourTurn) {
    picksUntil = 0
  }

  return {
    active: isSnake(root),
    round: clock.round,
    roundTotal: clock.roundTotal,
    seconds: clock.seconds,
    currentPick,
    onClockTeam,
    myTeam,
    isYourTurn,
    picksUntil,
    teamCount,
    headers,
    picklist,
    completedPicks,
  }
}

// ---------------------------------------------------------------------------
// Event diffing — emits the existing snake contract. ON CHANGE only.
// ---------------------------------------------------------------------------
export function initSnakeMemory() {
  return {
    wasYourTurn: false,
    lastPicksUntil: null,
    lastStatus: null,
    sentPickKeys: [], // dedupe snake_pick per board cell (roundPick)
  }
}

export function detectSnakeEvents(prev, curr) {
  const events = []
  const next = { ...prev, sentPickKeys: prev.sentPickKeys.slice() }

  // YOUR TURN — rising edge.
  if (curr.isYourTurn && !prev.wasYourTurn) {
    events.push({
      type: 'your_turn',
      platform: 'espn',
      payload: { round: curr.round, pick: curr.currentPick, picks_until_your_turn: 0 },
    })
  }

  // PICK COMING SOON — fire once at exactly 2 away.
  if (!curr.isYourTurn && curr.picksUntil === 2 && prev.lastPicksUntil !== 2) {
    events.push({
      type: 'your_turn_soon',
      platform: 'espn',
      payload: { picks_until_your_turn: 2, round: curr.round },
    })
  }

  // CONTINUOUS STATUS — on change.
  const status = {
    current_pick: curr.currentPick,
    current_round: curr.round,
    picks_until_your_turn: curr.picksUntil,
    on_clock_team: curr.onClockTeam,
    seconds_remaining: curr.seconds,
  }
  const ps = prev.lastStatus || {}
  if (
    status.current_pick !== ps.current_pick ||
    status.current_round !== ps.current_round ||
    status.picks_until_your_turn !== ps.picks_until_your_turn ||
    status.on_clock_team !== ps.on_clock_team ||
    status.seconds_remaining !== ps.seconds_remaining
  ) {
    events.push({ type: 'snake_status', platform: 'espn', payload: status })
  }

  // SNAKE PICK — every NEW completed board cell, deduped by its roundPick.
  for (const p of curr.completedPicks) {
    if (!p.name || !p.roundPick) continue
    if (next.sentPickKeys.includes(p.roundPick)) continue
    next.sentPickKeys.push(p.roundPick)
    const pickNumber =
      p.round != null && p.pickInRound != null && curr.teamCount
        ? (p.round - 1) * curr.teamCount + p.pickInRound
        : null
    events.push({
      type: 'snake_pick',
      platform: 'espn',
      payload: {
        pick_number: pickNumber,
        player_name: p.name,
        position: p.position,
        nfl_team: p.proTeam,
        espn_player_id: null,
        picker: p.team,
        is_yours: p.isMine,
        round: p.round,
      },
    })
  }

  next.wasYourTurn = curr.isYourTurn
  next.lastPicksUntil = curr.picksUntil
  next.lastStatus = status
  return { events, next }
}
