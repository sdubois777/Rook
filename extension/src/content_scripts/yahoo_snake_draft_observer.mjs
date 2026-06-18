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
 *   - "Last:\nJ. DOBBINS\n(RB · DEN)"                           (last pick made)
 */

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
 * back in on the following tick. Pick events come from console.error (handled
 * in the content script), not from here — this only watches turn/countdown.
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

/**
 * Build the snake_pick relay payload from a Yahoo console.error ['0'] frame
 * (['0', league, draft, pick_number, yahoo_player_id]) and the current parsed
 * state. Yahoo logs the ['0'] frame only for YOUR OWN picks (same as ['B']/['N']
 * in auction), so the pick is always attributed to you. Pure/testable.
 */
export function buildSnakePickPayload(frame, state) {
  return {
    pick_number: frame[3],
    yahoo_player_id: String(frame[4]),
    player_name: state?.lastPick?.player_name || null,
    position: state?.lastPick?.position || null,
    picker: 'You',
    is_yours: true,
    round: state?.currentRound ?? null,
  }
}
