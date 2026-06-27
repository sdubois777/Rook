/**
 * Sleeper SNAKE resolver → snake_pick / snake_status / your_turn / your_turn_soon.
 *
 * Event-driven (each WS frame is an event), not DOM-diffed. `player_picked`
 * carries full `metadata` (name/team/position) so picks map onto the contract
 * with no name resolution needed; `draft_updated_by_pick` carries the config
 * (teams, draft_order, reversal_round). Turn/status are DERIVED from the pick
 * stream + my slot — Sleeper has no explicit "on the clock" frame.
 *
 * `player_id` is a Sleeper id → exact match against Rook's indexed
 * `players.sleeper_id` (resolution_source = id_map). Pure → fixture-testable.
 */
import {
  isDraftFrame,
  readConfig,
  snakeSlot,
  roundOf,
  mySlotFrom,
  teamLabel,
} from './sleeper_resolve.mjs'

export function initSnakeMemory(myUserId) {
  return {
    myUserId: myUserId != null ? String(myUserId) : null,
    teams: null,
    reversalRound: 0,
    draftOrder: null,
    mySlot: null,
    sentPicks: [], // dedupe snake_pick by pick_no
    lastCompletedPick: null,
    lastCurrentPick: null,
    lastPicksUntil: null,
    wasYourTurn: false,
    lastSoon: null,
  }
}

function applyConfig(next, payload) {
  const c = readConfig(payload)
  if (c.teams != null) next.teams = c.teams
  if (c.reversalRound != null) next.reversalRound = c.reversalRound
  if (c.draftOrder) next.draftOrder = c.draftOrder
  const ms = mySlotFrom(next.draftOrder, next.myUserId)
  if (ms != null) next.mySlot = ms
}

/** Derive your_turn / your_turn_soon / snake_status from the last completed pick. */
function statusEvents(next) {
  const events = []
  if (next.lastCompletedPick == null || !next.teams) return events
  const currentPick = next.lastCompletedPick + 1
  const onClockSlot = snakeSlot(currentPick, next.teams, next.reversalRound)
  const isYourTurn = next.mySlot != null && onClockSlot === next.mySlot

  let picksUntil = isYourTurn ? 0 : null
  if (!isYourTurn && next.mySlot != null) {
    for (let p = currentPick; p < currentPick + next.teams * 2; p++) {
      if (snakeSlot(p, next.teams, next.reversalRound) === next.mySlot) {
        picksUntil = p - currentPick
        break
      }
    }
  }

  if (isYourTurn && !next.wasYourTurn) {
    events.push({
      type: 'your_turn',
      platform: 'sleeper',
      payload: { round: roundOf(currentPick, next.teams), pick: currentPick, picks_until_your_turn: 0 },
    })
  }
  if (!isYourTurn && picksUntil === 2 && next.lastSoon !== 2) {
    events.push({
      type: 'your_turn_soon',
      platform: 'sleeper',
      payload: { picks_until_your_turn: 2, round: roundOf(currentPick, next.teams) },
    })
  }
  if (currentPick !== next.lastCurrentPick || picksUntil !== next.lastPicksUntil) {
    events.push({
      type: 'snake_status',
      platform: 'sleeper',
      payload: {
        current_pick: currentPick,
        current_round: roundOf(currentPick, next.teams),
        picks_until_your_turn: picksUntil,
      },
    })
  }

  next.wasYourTurn = isYourTurn
  next.lastSoon = picksUntil
  next.lastCurrentPick = currentPick
  next.lastPicksUntil = picksUntil
  return events
}

export function detectSnakeEvents(memory, frame) {
  const events = []
  const next = { ...memory, sentPicks: memory.sentPicks.slice() }
  if (!isDraftFrame(frame)) return { events, next }
  const p = frame.payload || {}
  if (p.settings || p.draft_order) applyConfig(next, p)

  if (frame.event === 'player_picked') {
    const pickNo = p.pick_no
    if (pickNo != null && !next.sentPicks.includes(pickNo)) {
      next.sentPicks.push(pickNo)
      const slot = snakeSlot(pickNo, next.teams, next.reversalRound)
      const md = p.metadata || {}
      events.push({
        type: 'snake_pick',
        platform: 'sleeper',
        payload: {
          pick_number: pickNo,
          player_name: `${md.first_name || ''} ${md.last_name || ''}`.trim() || null,
          position: md.position || null,
          nfl_team: md.team || null,
          sleeper_player_id: p.player_id || null,
          picker: teamLabel(slot),
          is_yours: slot != null && slot === next.mySlot,
          round: roundOf(pickNo, next.teams),
        },
      })
      next.lastCompletedPick = Math.max(next.lastCompletedPick || 0, pickNo)
      events.push(...statusEvents(next))
    }
  } else if (frame.event.startsWith('draft_updated')) {
    // Config/status refresh — re-derive the live status (deduped internally).
    events.push(...statusEvents(next))
  }
  return { events, next }
}
