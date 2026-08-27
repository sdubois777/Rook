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
 * SELF-TEAM COMES FROM THE PICK TRAIN, NOT THE BOARD (#461). This file used to
 * read it ONLY from the board's `.myTeam` header, and its own comment claimed
 * "runtime always has the board". That claim was false and untested: the three
 * captures that would have covered it are truncated mid-tag at 4807 bytes, and
 * the one complete capture is from a MOCK draft. A customer in a REAL league
 * draft had no board grid in the page, and the consequences were exactly what
 * the dependency predicts —
 *   - own-team unresolvable  → isYourTurn could NEVER be true
 *                            → `your_turn` never fired
 *                            → the AI recommendation never ran, all draft
 *   - completed picks empty  → no `snake_pick`, so no pick was ever recorded
 *   - clock + pick train intact → `snake_status` kept working, so the round and
 *                                 pick numbers displayed correctly the whole time
 * which is precisely what they reported: "correctly indicating what round # and
 * pick # it was but nothing else", and "never populated recommended picks at all".
 *
 * ESPN already marks your own entries in the pick train, which does NOT depend on
 * the board being rendered: `own-pick` on the `[data-testid="current-pick"]`
 * widget means it is YOUR turn now, and `own-pick` on a `.pick-component` marks
 * an upcoming pick of yours. Those are the primary source now. The board is a
 * fallback for the display name, and remains the only source of completed picks —
 * that is inherent, since the board is where ESPN renders them — but its absence
 * no longer disables turn detection, the countdown, or the recommendation.
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

/**
 * Upcoming picks from the picklist → [{ pickNum, team, auto, isMine }].
 *
 * `isMine` is ESPN's own `own-pick` marker on the entry. It is what makes the
 * countdown to your next turn work without the draft board (#461).
 */
export function resolvePicklist(root) {
  const items = root ? Array.from(root.querySelectorAll('.picklist .pick-component')) : []
  return items
    .map((p) => ({
      pickNum: num(txt(p.querySelector('.pick-number'))),
      team: txt(p.querySelector('.team-name')) || null,
      auto: !!txt(p.querySelector('.auto-word')),
      isMine: p.classList ? p.classList.contains('own-pick') : false,
    }))
    .filter((p) => p.pickNum != null)
}

/**
 * Is the viewer on the clock RIGHT NOW, from the pick train alone?
 *
 * ESPN puts `own-pick` on a child of the current-pick widget when the turn is
 * yours. Board-independent, which is the whole point: comparing team NAMES
 * required the board to supply the viewer's own name, and returned false forever
 * when the board was not rendered.
 */
export function isOwnTurn(root) {
  const cp = root && root.querySelector('[data-testid="current-pick"]')
  if (!cp) return false
  return !!(cp.classList && cp.classList.contains('own-pick')) ||
    !!cp.querySelector('.own-pick')
}

/**
 * The viewer's own team DISPLAY NAME, without needing the board.
 *
 * Cosmetic only — attribution rides on the `own-pick` marker, never on this
 * string. Taken from the current-pick widget when the turn is yours, else from
 * the first pick-train entry marked as yours.
 */
export function resolveOwnTeamName(root) {
  if (!root) return null
  const cp = root.querySelector('[data-testid="current-pick"]')
  if (cp && isOwnTurn(root)) {
    const name = cp.getAttribute('title') || txt(cp.querySelector('.team-name'))
    if (name) return name
  }
  const mine = root.querySelector('.picklist .pick-component.own-pick')
  if (mine) {
    const name = mine.getAttribute('title') || txt(mine.querySelector('.team-name'))
    if (name) return name
  }
  return null
}

/**
 * Full snake state. `opts.myTeam` supplies the own-team DISPLAY NAME when the
 * page cannot (partial captures); it never decides whose turn it is.
 */
export function resolveSnakeState(root, opts = {}) {
  const clock = resolveClock(root)
  const cp = root && root.querySelector('[data-testid="current-pick"]')
  const onClockText = cp ? txt(cp.querySelector('.on-the-clock')) : ''
  const onClockTeam =
    (cp && (cp.getAttribute('title') || txt(cp.querySelector('.team-name')))) || null
  const currentPick = num(onClockText) // "On the Clock: Pick 11" → 11
  const headers = resolveBoardHeaders(root)
  const picklist = resolvePicklist(root)
  const completedPicks = resolveCompletedPicks(root, headers)
  const teamCount = headers.length || null

  // OWN-TEAM NAME: pick train first, board second, caller-supplied last. Only
  // the display label — nothing below decides anything from it (#461).
  const myTeam = resolveOwnTeamName(root) || resolveMyTeam(root) || opts.myTeam || null

  // WHOSE TURN IT IS comes from ESPN's own `own-pick` marker, NOT from comparing
  // team names. The name comparison needed the board to supply the viewer's own
  // name, so with no board it was false forever and `your_turn` never fired —
  // which is why the AI recommendation never ran for a whole draft (#461).
  // The name comparison is kept as a fallback for a page that somehow lacks the
  // marker but does identify both sides.
  const isYourTurn = isOwnTurn(root) ||
    !!(myTeam && onClockTeam && onClockTeam === myTeam)

  // Picks until my next turn = my next pick-train entry − the current pick.
  // Prefer ESPN's `own-pick` marker; fall back to matching the display name.
  let picksUntil = null
  if (isYourTurn) {
    picksUntil = 0
  } else if (currentPick != null) {
    const mine = picklist.find((p) => p.isMine) ||
      (myTeam ? picklist.find((p) => p.team === myTeam) : null)
    if (mine) picksUntil = mine.pickNum - currentPick
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

  // SNAKE PICKS FIRST — every NEW completed board cell, deduped by its
  // roundPick. Emitted BEFORE your_turn so the backend records the pick before
  // generating the on-the-clock recommendation — a pick landing in the same
  // tick as your turn (picked right before you) was otherwise still "available"
  // to the engine and it could recommend a just-drafted player.
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
    // your_team_name = derived own-team DISPLAY name (from the .myTeam board
    // header); the backend upgrades the generic label from it. Additive/cosmetic,
    // kept out of the change-detection `status` object above.
    events.push({
      type: 'snake_status', platform: 'espn',
      payload: { ...status, your_team_name: curr.myTeam || null },
    })
  }

  next.wasYourTurn = curr.isYourTurn
  next.lastPicksUntil = curr.picksUntil
  next.lastStatus = status
  return { events, next }
}
