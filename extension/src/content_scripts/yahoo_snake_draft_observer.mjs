/**
 * Pure parsing + event-detection helpers for the Yahoo SNAKE draft poller.
 *
 * Kept free of any browser/DOM/network side effects so the logic is
 * unit-testable in plain Node (see extension/test/yahoo_snake_draft.test.mjs).
 * The content script (yahoo_snake_draft.js) supplies the live `#app` innerText
 * and relays the detected events; everything here is a pure function.
 *
 * Snake DOM (confirmed from a live June 2026 session) differs from auction:
 *   - root is `#app` (not `#draft`), no bids, no per-player clock
 *   - "Bart's Pick • You're up in 9 Picks • Round 7, Pick 84"  (someone else up)
 *   - "YOUR TURN • ROUND 8, PICK 93"                            (you on the clock)
 *   - the "Picks" panel holds the COMPLETE, ordered pick history (parsePicks)
 */

// Defensive positions kept so a misparsed card can't masquerade as a pick.
const VALID_POSITIONS = new Set([
  'QB', 'RB', 'WR', 'TE', 'K', 'DEF', 'DST', 'DB', 'LB', 'DL',
])

/**
 * True when `#app` text carries live SNAKE-draft markers (the turn / countdown
 * banner). Used to POSITIVELY confirm a snake draft before the poller takes any
 * page-mutating action: auction rooms share our URL match patterns AND also
 * have `#app`, so "#app exists" is not enough to know we're on a snake page.
 */
export function hasSnakeMarkers(text) {
  const s = parseSnakeState(text)
  return (
    !!s &&
    (s.isYourTurn || s.currentPicker != null || s.picksUntilYourTurn != null)
  )
}

/**
 * Gate for the snake poller. It may act ONLY on a confirmed snake draft:
 *   - NEVER when the auction nomination panel (#draft) is present, and
 *   - only when snake markers are visible in #app text.
 *
 * Both pollers share Yahoo's draft URL patterns, so each MUST positively detect
 * its own draft type before acting. The snake poller's clickPicksTab() mutates
 * the page — on an auction room that click switches the view and removes
 * #draft, starving the auction poller (the cross-poller-interference outage).
 * Pure: the content script passes the live #draft presence + #app text.
 */
export function shouldSnakeActivate({ hasDraftPanel, appText }) {
  if (hasDraftPanel) return false
  return hasSnakeMarkers(appText || '')
}

/**
 * Find the "Picks" tab button among a list of button-like elements. The pick
 * cards only render once the Picks tab is active, so the content script clicks
 * this. Pure: takes the array of buttons (content script passes the live ones).
 */
export function findPicksButton(buttons) {
  return (
    (buttons || []).find((b) => (b && b.innerText ? b.innerText : '').trim() === 'Picks') ||
    null
  )
}

/**
 * Parse pick cards into an ordered pick list.
 *
 * Each card's innerText (confirmed live) is newline-separated:
 *   parts[0] = pick number (integer)   parts[3] = position (QB/RB/WR/TE/...)
 *   parts[1] = team ("You" for yours)  parts[4] = NFL team abbreviation
 *   parts[2] = player name             parts[5] = "Bye {N}" (optional)
 *
 * Pure/testable: takes an array of card innerText strings (the content script
 * collects these via querySelectorAll on the pick-card CSS selector — that DOM
 * read is the only impure part and lives in yahoo_snake_draft.js). The CSS
 * selector targets the cards directly, so this avoids the false positives the
 * old #app innerText scan hit (expert ranks, stat columns, other integers).
 */
export function parsePickCards(cardTexts) {
  const picks = []
  for (const text of cardTexts || []) {
    const parts = (text || '')
      .trim()
      .split('\n')
      .map((p) => p.trim())
      .filter((p) => p.length > 0)
    if (parts.length < 4) continue

    const pickNum = parseInt(parts[0], 10)
    // parts[0] must be PURELY the pick number (not "Round 4" or a stat).
    if (Number.isNaN(pickNum) || pickNum <= 0 || String(pickNum) !== parts[0]) {
      continue
    }
    const position = parts[3]
    if (!VALID_POSITIONS.has(position)) continue

    picks.push({
      pick_number: pickNum,
      team: parts[1],
      player_name: parts[2],
      position,
      nfl_team: parts[4] || null,
      is_yours: parts[1] === 'You',
    })
  }
  // DOM order should already be correct, but sort defensively by pick number.
  return picks.sort((a, b) => a.pick_number - b.pick_number)
}

/**
 * Parse the snake draft state out of `#app` innerText.
 *
 * Returns { isYourTurn, yourRound, yourPick, currentPicker, currentRound,
 * currentPick, picksUntilYourTurn, lastPick } or null when text is empty.
 */
export function parseSnakeState(text) {
  if (!text) return null

  const lines = text
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0)

  // YOUR TURN — "YOUR TURN • ROUND 8, PICK 93"
  const yourTurnLine = lines.find((l) => l.toUpperCase().startsWith('YOUR TURN'))
  const yourTurnMatch = yourTurnLine
    ? yourTurnLine.match(/YOUR TURN\s*[•·]\s*ROUND\s+(\d+),\s*PICK\s+(\d+)/i)
    : null
  const isYourTurn = !!yourTurnMatch
  const yourRound = yourTurnMatch ? parseInt(yourTurnMatch[1], 10) : null
  const yourPick = yourTurnMatch ? parseInt(yourTurnMatch[2], 10) : null

  // Someone else picking —
  // "Bart's Pick • You're up in 9 Picks • Round 7, Pick 84"
  const pickingLine = lines.find(
    (l) => l.includes("'s Pick") && l.includes('Round')
  )
  const pickingMatch = pickingLine
    ? pickingLine.match(/^(.+?)'s Pick.*Round\s+(\d+),\s*Pick\s+(\d+)/i)
    : null
  const currentPicker = pickingMatch ? pickingMatch[1] : null
  const currentRound = pickingMatch ? parseInt(pickingMatch[2], 10) : yourRound
  const currentPick = pickingMatch ? parseInt(pickingMatch[3], 10) : yourPick

  // Picks until your turn — "You're up in 9 Picks"
  const upInMatch = text.match(/You're up in (\d+) Pick/i)
  const picksUntilYourTurn = upInMatch
    ? parseInt(upInMatch[1], 10)
    : isYourTurn
    ? 0
    : null

  // Last pick made — "Last:\nJ. DOBBINS\n(RB · DEN)"
  const lastIdx = lines.findIndex((l) => l === 'Last:')
  let lastPick = null
  if (lastIdx >= 0 && lines[lastIdx + 1]) {
    const playerName = lines[lastIdx + 1]
    const posTeam = lines[lastIdx + 2]
      ? lines[lastIdx + 2].match(/\(([A-Z]+)\s*[·•]\s*([A-Z]+)\)/)
      : null
    lastPick = {
      player_name: playerName,
      position: posTeam ? posTeam[1] : null,
      team: posTeam ? posTeam[2] : null,
    }
  }

  return {
    isYourTurn,
    yourRound,
    yourPick,
    currentPicker,
    currentRound,
    currentPick,
    picksUntilYourTurn,
    lastPick,
  }
}

/**
 * Diff the previous tracked memory against the freshly parsed state and return
 * the snake events to relay plus the updated memory.
 *
 * `prev` carries { wasYourTurn, lastPicksUntil }. Pure: callers thread `next`
 * back in on the following tick. This watches turn/countdown ONLY — the full
 * pick history (yours and opponents) comes from parsePicks(), which is far more
 * reliable than the single "Last:" line ever was.
 */
export function detectSnakeEvents(prev, curr) {
  const events = []
  const next = { ...prev }
  if (!curr) return { events, next }

  // YOUR TURN — rising edge of the on-the-clock state.
  if (curr.isYourTurn && !prev.wasYourTurn) {
    events.push({
      type: 'your_turn',
      platform: 'yahoo',
      payload: {
        round: curr.yourRound,
        pick: curr.yourPick,
        picks_until_your_turn: 0,
      },
    })
  }

  // PICK COMING SOON — fire once when exactly 2 picks away.
  if (
    !curr.isYourTurn &&
    curr.picksUntilYourTurn === 2 &&
    prev.lastPicksUntil !== 2
  ) {
    events.push({
      type: 'your_turn_soon',
      platform: 'yahoo',
      payload: { picks_until_your_turn: 2, round: curr.currentRound },
    })
  }

  // CONTINUOUS STATUS — additive. Yahoo already renders the current pick/round
  // and "You're up in N" (snake-reversal-correct) every poll; we parse it but
  // previously only emitted at the 0/2 alert boundaries, so the status line went
  // stale between them. Emit whenever any of these change, so the panel is always
  // current — not just at the alert moments. This is STATUS DATA, not an alert:
  // the your_turn / your_turn_soon alerts above are unchanged.
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
  next.lastStatus = status

  next.wasYourTurn = curr.isYourTurn
  next.lastPicksUntil = curr.picksUntilYourTurn
  return { events, next }
}
