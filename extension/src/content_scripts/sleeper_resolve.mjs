/**
 * Shared Sleeper draft helpers — Phoenix Channels over WebSocket.
 *
 * Sleeper's draft room is an Elixir/Phoenix app: real-time draft state arrives as
 * WS frames in the Phoenix v2 wire format — a 5-tuple array
 * `[join_ref, ref, topic, event, payload]`. There are NO DOM selectors to parse;
 * the whole draft is in clean JSON on the `draft:<draft_id>` topic. That makes
 * Sleeper the most robust target (nothing to break on a UI redeploy — only the
 * Phoenix event names could change).
 *
 * These helpers are pure (frame in → data out), so the resolvers are unit-tested
 * by replaying captured frames (test/fixtures/sleeper/).
 */

export const DRAFT_TOPIC_RE = /^draft:(\d+)$/

/** Parse one Phoenix WS frame string → {joinRef, ref, topic, event, payload}, or null. */
export function parseFrame(data) {
  let arr
  try {
    arr = typeof data === 'string' ? JSON.parse(data) : data
  } catch {
    return null
  }
  if (!Array.isArray(arr) || arr.length < 5) return null
  const [joinRef, ref, topic, event, payload] = arr
  if (typeof topic !== 'string' || typeof event !== 'string') return null
  return { joinRef, ref, topic, event, payload: payload || {} }
}

/** True only for the live draft channel (NOT presence_draft:* / phoenix heartbeats). */
export function isDraftFrame(frame) {
  return !!frame && DRAFT_TOPIC_RE.test(frame.topic || '')
}

export function draftIdFromTopic(topic) {
  const m = DRAFT_TOPIC_RE.exec(topic || '')
  return m ? m[1] : null
}

export const roundOf = (pickNo, teams) => (teams ? Math.ceil(pickNo / teams) : null)

/**
 * SERPENTINE draft slot (1-based) for a global pick number.
 * Standard snake: round 1 forward, even rounds reverse. `reversalRound` models
 * Sleeper's "Nth-round reversal" option (e.g. 3 → rounds ≥3 flip parity again);
 * 0 = standard. (Verified for reversal_round=0; the flip for >0 is asserted from
 * the rule — re-verify against a real reversal capture.)
 */
export function snakeSlot(pickNo, teams, reversalRound = 0) {
  if (!teams) return null
  const round = roundOf(pickNo, teams)
  const idx = (pickNo - 1) % teams
  let forward = round % 2 === 1
  if (reversalRound && round >= reversalRound) forward = !forward
  return forward ? idx + 1 : teams - idx
}

/**
 * Unwrap the localStorage `user_id` to the BARE id that keys `draft_order`.
 * Sleeper JSON-encodes it (`"\"1373…\""`), so a raw read carries literal quotes
 * and never matches the draft_order key. Parse only when it's actually quoted /
 * object-wrapped — NEVER a bare numeric string (a 19-digit id exceeds
 * Number.MAX_SAFE_INTEGER, so JSON.parse would silently lose precision).
 */
export function parseUserId(raw) {
  if (raw == null) return null
  const s = String(raw).trim()
  if (!s) return null
  if (s[0] === '"' || s[0] === '{' || s[0] === '[') {
    try {
      const parsed = JSON.parse(s)
      if (typeof parsed === 'string') return parsed.trim() || null
      if (parsed && typeof parsed === 'object') {
        const id = parsed.user_id ?? parsed.id
        return id != null ? String(id).trim() || null : null
      }
    } catch {
      // not valid JSON — fall through to the raw string
    }
  }
  return s
}

/** My draft slot from draft_order {user_id: slot}; null if I'm not mapped yet. */
export function mySlotFrom(draftOrder, myUserId) {
  if (!draftOrder || myUserId == null) return null
  const v = draftOrder[String(myUserId)]
  return v == null ? null : Number(v)
}

/** Show_team_names is off in mocks → anonymous "Team N" by slot. */
export const teamLabel = (slot) => (slot == null ? null : `Team ${slot}`)

/** Pull config (teams/reversal/order/status/timers) off any full-state frame. */
export function readConfig(payload) {
  const s = payload.settings || {}
  return {
    type: payload.type ?? null, // snake | auction | linear
    status: payload.status ?? null, // drafting | complete | paused
    teams: s.teams ?? null,
    rounds: s.rounds ?? null,
    reversalRound: s.reversal_round ?? 0,
    pickTimer: s.pick_timer ?? null,
    budget: s.budget ?? null,
    draftOrder: payload.draft_order || null,
    lastPicked: payload.last_picked ?? null,
  }
}
