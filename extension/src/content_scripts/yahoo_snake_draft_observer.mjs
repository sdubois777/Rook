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

  next.wasYourTurn = curr.isYourTurn
  next.lastPicksUntil = curr.picksUntilYourTurn
  return { events, next }
}
