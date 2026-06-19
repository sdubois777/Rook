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

// Defensive positions kept so a misparsed line can't masquerade as a pick.
const VALID_POSITIONS = new Set([
  'QB', 'RB', 'WR', 'TE', 'K', 'DEF', 'DST', 'DB', 'LB', 'DL',
])

/**
 * Parse the complete draft history from Yahoo's "Picks" panel.
 *
 * Confirmed structure (live session): after the "Picks" header, each pick is
 * five lines — a PURE INTEGER pick number, then team, player, position, nfl_team
 * — followed by 1-4 upcoming-team-order lines (noise) before the next pick
 * number. YOUR team is always shown as "You". Scan for pure integers; read the
 * next four lines as a pick; validate the position field; skip everything else.
 *
 * Pure/testable: takes the `#app` innerText, returns an ordered list of picks.
 */
export function parsePicks(appText) {
  if (!appText) return []

  const lines = appText
    .split('\n')
    .map((l) => l.trim())
    .filter(
      (l) =>
        l.length > 0 &&
        !l.endsWith(' joined') &&
        !l.endsWith(' left') &&
        !l.startsWith('Bye ')
    )

  const picksIdx = lines.findIndex((l) => l === 'Picks')
  if (picksIdx === -1) return []

  const body = lines.slice(picksIdx + 1)
  const picks = []
  let i = 0
  while (i < body.length) {
    const num = parseInt(body[i], 10)
    // A pick starts at a PURE integer (the pick number). Team names and player
    // names never stringify back to themselves as an integer.
    if (!Number.isNaN(num) && num > 0 && String(num) === body[i]) {
      const team = body[i + 1]
      const player = body[i + 2]
      const position = body[i + 3]
      const nflTeam = body[i + 4]
      if (team && player && position && VALID_POSITIONS.has(position)) {
        picks.push({
          pick_number: num,
          team,
          player_name: player,
          position,
          nfl_team: nflTeam || null,
          is_yours: team === 'You',
        })
        i += 5
        continue
      }
    }
    i += 1
  }
  return picks
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

  next.wasYourTurn = curr.isYourTurn
  next.lastPicksUntil = curr.picksUntilYourTurn
  return { events, next }
}
