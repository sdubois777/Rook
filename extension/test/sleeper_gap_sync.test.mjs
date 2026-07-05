import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createGapReconciler } from '../src/content_scripts/sleeper_gap_sync.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))

// --- deterministic fake clock (setTimeout/clearTimeout + microtask flush) ---
function makeClock() {
  let now = 0
  let seq = 0
  let timers = []
  const flush = () => new Promise((r) => setImmediate(r))
  return {
    setTimeout: (fn, ms) => {
      const id = ++seq
      timers.push({ id, at: now + ms, fn })
      return id
    },
    clearTimeout: (id) => {
      timers = timers.filter((t) => t.id !== id)
    },
    async advance(ms) {
      await flush() // settle any pending sync-completion microtask (e.g. after release)
      const target = now + ms
      timers.sort((a, b) => a.at - b.at || a.id - b.id)
      while (timers.length && timers[0].at <= target) {
        const t = timers.shift()
        now = t.at
        t.fn()
        await flush() // let sync()'s promise chain settle before firing the next
        timers.sort((a, b) => a.at - b.at || a.id - b.id)
      }
      now = target
      await flush()
    },
  }
}

function makeReconciler(overrides = {}) {
  const clock = makeClock()
  const calls = { n: 0 }
  const warns = []
  let userId = overrides.userId !== undefined ? overrides.userId : 'user-1'
  const gate = overrides.hang ? [] : null // array of resolvers if sync should hang
  const gr = createGapReconciler({
    sync: async () => {
      calls.n += 1
      if (gate) await new Promise((r) => gate.push(r))
    },
    getUserId: () => userId,
    warn: (m) => warns.push(m),
    setTimeoutFn: clock.setTimeout,
    clearTimeoutFn: clock.clearTimeout,
    debounceMs: 350,
    userRetryMs: 500,
    ...overrides.opts,
  })
  return {
    gr,
    clock,
    calls,
    warns,
    releaseOne: () => gate && gate.shift()?.(),
    setUser: (u) => {
      userId = u
    },
  }
}

test('a single pick signal triggers one REST sync after the debounce', async () => {
  const { gr, clock, calls } = makeReconciler()
  gr.onPickSignal()
  await clock.advance(349)
  assert.equal(calls.n, 0) // still debouncing
  await clock.advance(1)
  assert.equal(calls.n, 1) // fired at 350ms
})

test('a burst within the debounce window collapses to ONE sync', async () => {
  const { gr, clock, calls } = makeReconciler()
  for (let i = 0; i < 10; i++) gr.onPickSignal()
  await clock.advance(400)
  assert.equal(calls.n, 1)
})

test('single-in-flight coalescing: signals during a sync produce exactly ONE more', async () => {
  const { gr, clock, calls, releaseOne } = makeReconciler({ hang: true })
  gr.onPickSignal()
  await clock.advance(350) // sync #1 starts, hangs
  assert.equal(calls.n, 1)

  // A whole autopick burst lands while #1 is in flight.
  for (let i = 0; i < 8; i++) gr.onPickSignal()
  await clock.advance(350)
  assert.equal(calls.n, 1) // still just #1 — the burst set the dirty flag only

  releaseOne() // #1 completes → dirty → schedule exactly one follow-up
  await clock.advance(350)
  assert.equal(calls.n, 2) // exactly one more, not eight

  releaseOne()
  await clock.advance(350)
  assert.equal(calls.n, 2) // no phantom third run
})

test('null user_id DEFERS the attributing sync, then fires once it resolves', async () => {
  const h = makeReconciler({ userId: null })
  h.gr.onPickSignal()
  await h.clock.advance(350)
  assert.equal(h.calls.n, 0) // did NOT attribute-sync under an unknown user
  assert.equal(h.warns.length, 1)
  assert.match(h.warns[0], /DEFERRED/)

  await h.clock.advance(500) // retry — still null → no 2nd warn (once per episode)
  assert.equal(h.calls.n, 0)
  assert.equal(h.warns.length, 1)

  h.setUser('user-1') // id resolves
  await h.clock.advance(500) // next retry fires the sync
  assert.equal(h.calls.n, 1)
})

test('null user_id gives up the fast lane after maxUserRetries (idle net backstops)', async () => {
  const h = makeReconciler({ userId: null, opts: { maxUserRetries: 3, userRetryMs: 100 } })
  h.gr.onPickSignal()
  await h.clock.advance(350) // fire #1 (retry 1)
  await h.clock.advance(100) // retry 2
  await h.clock.advance(100) // retry 3
  await h.clock.advance(100) // retry 4 > max → give up + loud warn, stop retrying
  assert.equal(h.calls.n, 0)
  assert.match(h.warns.at(-1), /backstop/)
  // No further retries scheduled.
  await h.clock.advance(1000)
  assert.equal(h.calls.n, 0)
})

test('concurrency: a 26-frame draft-start burst loses no pick and stays coalesced', async () => {
  // Drive one signal per draft_updated_by_pick frame from the REAL capture.
  const frames = JSON.parse(
    readFileSync(join(__dirname, 'fixtures/sleeper/snake-draft-start.json'), 'utf8')
  )
  const pickSignals = frames.filter((f) => f[3] === 'draft_updated_by_pick').length
  assert.equal(pickSignals, 26) // ground truth: once-per-pick, 26 picks

  const { gr, clock, calls, releaseOne } = makeReconciler({ hang: true })

  // Half the burst lands close together (batched by the debounce) → 1 sync starts.
  const half = Math.floor(pickSignals / 2)
  for (let i = 0; i < half; i++) gr.onPickSignal()
  await clock.advance(350)
  assert.equal(calls.n, 1)

  // The rest land WHILE that sync is in flight → collapse to the dirty flag.
  for (let i = half; i < pickSignals; i++) gr.onPickSignal()
  await clock.advance(350)
  assert.equal(calls.n, 1) // no per-frame syncs

  releaseOne() // in-flight completes → exactly one follow-up for the whole tail
  await clock.advance(350)
  assert.equal(calls.n, 2)
  releaseOne()
  await clock.advance(1000)

  // 26 rapid signals cost 2 REST syncs — and a sync ran AFTER the last signal,
  // so the authoritative REST fill sees every pick (loses none).
  assert.equal(calls.n, 2, `coalesced 26 signals to ${calls.n} syncs`)
})
