/**
 * Sleeper SNAKE gap-triggered reconciliation scheduler (pure, injectable).
 *
 * Sleeper does NOT emit a `player_picked` frame for every pick — in a real
 * draft-start capture, 10 of 26 picks (including opening picks 1/2/3, all CPU
 * autopicks) fired ONLY `draft_updated_by_pick`, which carries no player
 * identity. Those picks are invisible to the resolver. But `draft_updated_by_pick`
 * fires exactly once per pick, so it's a reliable "a pick happened" signal, and
 * the #198 REST backfill (GET /v1/draft/{id}/picks) is authoritative (player_id +
 * pick_no for every pick, autopicks included).
 *
 * This turns the blind ≤30s poll into a fast, pick-driven reconciliation: each
 * `draft_updated_by_pick` schedules a REST sync, batched by a short trailing
 * debounce and collapsed by single-in-flight coalescing so an autopick burst
 * (~1s cadence, can be faster) costs minimal REST calls.
 *
 * NULL-USER TRAP (closed here): if the viewer's Sleeper user_id isn't resolved
 * yet, a reconciliation would attribute EVERY pick is_yours=False and file your
 * own autopicked picks onto opponent rosters — and because backend is_drafted
 * dedup no-ops later (correct) syncs, that mis-attribution never self-corrects.
 * So a reconciliation is DEFERRED (short backoff, loud warn) until the id
 * resolves; the idle nets (30s poll / channel-entry) remain the backstop.
 *
 * Pure: all timers, the sync call, the user-id read, and the warn sink are
 * injected, so the debounce / coalescing / defer logic is unit-tested with fake
 * timers and stubs (no browser, no network).
 */

export function createGapReconciler({
  sync, // () => Promise<any> — performs ONE REST reconciliation (syncDraftState)
  getUserId, // () => string|null — my_user_id availability (memoized upstream)
  warn = () => {}, // (msg) => void — loud, no-silent-discard
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  debounceMs = 350, // batch near-simultaneous frames
  userRetryMs = 500, // backoff while waiting for user_id to resolve
  maxUserRetries = 20, // ~10s of fast-lane retries, then defer to the idle net
} = {}) {
  if (typeof sync !== 'function' || typeof getUserId !== 'function') {
    throw new Error('createGapReconciler requires sync + getUserId functions')
  }

  let debounceTimer = null
  let inFlight = false
  let dirty = false // a pick arrived while a sync was running → run once more
  let userRetries = 0
  let deferWarned = false // warn once per deferral episode, not per retry

  function schedule() {
    if (debounceTimer != null) return // already armed within this window
    debounceTimer = setTimeoutFn(fire, debounceMs)
  }

  function fire() {
    debounceTimer = null

    // NULL-USER GATE — never run an attributing reconciliation without a user id.
    if (getUserId() == null) {
      userRetries += 1
      if (userRetries <= maxUserRetries) {
        if (!deferWarned) {
          deferWarned = true
          warn(
            'Rook Sleeper: pick reconciliation DEFERRED — user_id not resolved yet; ' +
              'retrying so recovered own-picks are not mis-filed to opponents.'
          )
        }
        debounceTimer = setTimeoutFn(fire, userRetryMs)
        return
      }
      // Gave up the fast lane — the idle 30s / channel-entry sync still backstops.
      warn(
        'Rook Sleeper: user_id still unresolved after retries — fast-lane ' +
          'reconciliation skipped; the periodic sync will backstop once it resolves.'
      )
      userRetries = 0
      deferWarned = false
      return
    }
    userRetries = 0
    deferWarned = false

    // SINGLE-IN-FLIGHT COALESCING — collapse a burst into minimal REST calls.
    if (inFlight) {
      dirty = true
      return
    }
    inFlight = true
    Promise.resolve()
      .then(sync)
      .catch(() => {}) // a failed sync is retried by the next signal / idle net
      .then(() => {
        inFlight = false
        if (dirty) {
          dirty = false
          schedule() // exactly one more run for everything seen mid-flight
        }
      })
  }

  return {
    /** Call on each `draft_updated_by_pick` frame (the "a pick happened" signal). */
    onPickSignal() {
      schedule()
    },
    /** Test/introspection only. */
    _state() {
      return { inFlight, dirty, scheduled: debounceTimer != null, userRetries }
    },
  }
}
