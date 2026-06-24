import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseHTML } from 'linkedom'

import {
  snakeRoot,
  resolveSnakeBanner,
  resolveSnakeState,
  resolveTeamOrder,
  resolveLastPick,
  pickSlotIndex,
  hasSnakeContent,
  shouldSnakeActivate,
  detectSnakeEvents,
  initSnakeMemory,
} from '../src/content_scripts/yahoo_snake_resolve.mjs'
import { shouldAuctionActivate } from '../src/content_scripts/yahoo_auction_resolve.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIX = join(__dirname, 'fixtures', 'auction')

// Parse a captured Yahoo outerHTML fixture into a document (+ React root).
function docFor(name) {
  const { document } = parseHTML(readFileSync(join(FIX, name), 'utf-8'))
  return document
}
function rootFor(name) {
  return snakeRoot(docFor(name))
}

const manifest = JSON.parse(
  readFileSync(join(__dirname, '..', 'manifest.json'), 'utf-8')
)
const snakeSrc = readFileSync(
  join(__dirname, '..', 'src', 'content_scripts', 'yahoo_snake_draft.js'),
  'utf-8'
)

test('yahoo_snake_draft.js in manifest matches the Yahoo draft URL', () => {
  const cs = manifest.content_scripts.find((c) =>
    c.js.includes('yahoo_snake_draft.js')
  )
  assert.ok(cs, 'snake content script missing from manifest')
  assert.ok(
    cs.matches.some((m) => m.includes('football.fantasysports.yahoo.com')),
    'snake content script does not match the Yahoo draft domain'
  )
})

// ---------------------------------------------------------------------------
// resolveSnakeBanner — the turn banner is the snake content signal AND the
// source of round/pick/countdown. Parsed off SHORT spans (not the catch-all
// container blob, which would mis-concatenate).
// ---------------------------------------------------------------------------
test('banner: "Your Turn" — on the clock now (snake-onclock.html)', () => {
  const b = resolveSnakeBanner(rootFor('snake-onclock.html'))
  assert.deepEqual(b, {
    isYourTurn: true,
    round: 1,
    pick: 11,
    picker: 'You',
    picksUntilYourTurn: 0,
  })
})

test('banner: opponent on the clock, countdown N (snake-waiting.html)', () => {
  const b = resolveSnakeBanner(rootFor('snake-waiting.html'))
  assert.deepEqual(b, {
    isYourTurn: false,
    round: 1,
    pick: 3,
    picker: 'Michael',
    picksUntilYourTurn: 8,
  })
})

test('banner: two picks out (snake-postpick.html)', () => {
  const b = resolveSnakeBanner(rootFor('snake-postpick.html'))
  assert.equal(b.isYourTurn, false)
  assert.equal(b.picker, 'Jermaine')
  assert.equal(b.currentPick ?? b.pick, 12)
  assert.equal(b.picksUntilYourTurn, 2)
})

test('banner: null on an auction page (no snake turn banner)', () => {
  assert.equal(resolveSnakeBanner(snakeRoot(docFor('nomination.html'))), null)
  assert.equal(resolveSnakeBanner(snakeRoot(docFor('lobby.html'))), null)
})

// ---------------------------------------------------------------------------
// resolveTeamOrder — round-1 draft order from the first N team-header cells.
// ---------------------------------------------------------------------------
test('teamOrder: 12 teams in draft order, the viewer keyed "You"', () => {
  const order = resolveTeamOrder(rootFor('snake-onclock.html'))
  assert.equal(order.length, 12)
  assert.equal(order[10], 'You') // viewer drafts 11th
  assert.equal(order[9], 'Koby')
  assert.equal(order[0], 'AV')
})

// ---------------------------------------------------------------------------
// pickSlotIndex — SERPENTINE ordering. The captures only exercise an early
// forward Round 1, so the round-boundary cases are asserted from the snake rule
// itself (flag for a real round-turn capture to lock).
// ---------------------------------------------------------------------------
test('serpentine: round 1 runs forward', () => {
  assert.equal(pickSlotIndex(1, 12), 0)
  assert.equal(pickSlotIndex(10, 12), 9)
  assert.equal(pickSlotIndex(12, 12), 11)
})

test('serpentine: ROUND BOUNDARY reverses — pick 12 and pick 13 are the SAME team', () => {
  // 12-team league: last of round 1 picks again first in round 2.
  assert.equal(pickSlotIndex(13, 12), 11)
  assert.equal(pickSlotIndex(12, 12), pickSlotIndex(13, 12))
  // ...and the round-2/3 boundary mirrors it (pick 24 == pick 25 → slot 0).
  assert.equal(pickSlotIndex(24, 12), 0)
  assert.equal(pickSlotIndex(25, 12), 0)
  // Mid round 2 runs in reverse.
  assert.equal(pickSlotIndex(14, 12), 10)
  assert.equal(pickSlotIndex(23, 12), 1)
})

test('serpentine: picker maps correctly across a synthetic round boundary', () => {
  // Construct the expectation from the snake rule on the real draft order.
  const order = resolveTeamOrder(rootFor('snake-onclock.html'))
  const last = order[order.length - 1] // 'Jermaine' — drafts 12th
  assert.equal(order[pickSlotIndex(12, 12)], last) // R1 last
  assert.equal(order[pickSlotIndex(13, 12)], last) // R2 first — same team
})

// ---------------------------------------------------------------------------
// resolveLastPick / resolveSnakeState — the just-completed pick from the
// "Last:" indicator + the serpentine board (currentPick − 1).
// ---------------------------------------------------------------------------
test('lastPick: opponent pick resolves player/pos/team/picker (snake-onclock)', () => {
  const st = resolveSnakeState(rootFor('snake-onclock.html'))
  assert.deepEqual(st.lastPick, {
    pick_number: 10,
    player_name: 'C. Brown',
    position: 'RB',
    nfl_team: 'Cin',
    picker: 'Koby',
    is_yours: false,
    round: 1,
  })
})

test('lastPick: YOUR pick is tagged is_yours via the board "You" slot (snake-postpick)', () => {
  const st = resolveSnakeState(rootFor('snake-postpick.html'))
  assert.equal(st.lastPick.pick_number, 11)
  assert.equal(st.lastPick.player_name, 'C. Lamb')
  assert.equal(st.lastPick.position, 'WR')
  assert.equal(st.lastPick.nfl_team, 'Dal')
  assert.equal(st.lastPick.picker, 'You')
  assert.equal(st.lastPick.is_yours, true)
})

test('lastPick: null when the current pick is the first of the draft', () => {
  // A pick-1 banner has no prior pick → resolveLastPick returns null.
  const order = resolveTeamOrder(rootFor('snake-onclock.html'))
  assert.equal(resolveLastPick(rootFor('snake-onclock.html'), { pick: 1 }, order), null)
})

test('snakeState: full board state for an opponent-on-clock capture', () => {
  const st = resolveSnakeState(rootFor('snake-waiting.html'))
  assert.equal(st.isYourTurn, false)
  assert.equal(st.currentPick, 3)
  assert.equal(st.currentRound, 1)
  assert.equal(st.currentPicker, 'Michael')
  assert.equal(st.picksUntilYourTurn, 8)
  assert.equal(st.teamCount, 12)
  assert.equal(st.lastPick.player_name, 'J. Chase')
})

// ---------------------------------------------------------------------------
// detectSnakeEvents — emits the SAME events the backend/frontend handle.
// ---------------------------------------------------------------------------
test('events: your_turn fires on the rising edge only', () => {
  const curr = resolveSnakeState(rootFor('snake-onclock.html'))
  const first = detectSnakeEvents(initSnakeMemory(), curr)
  const yt = first.events.find((e) => e.type === 'your_turn')
  assert.ok(yt, 'your_turn fires on the rising edge')
  assert.deepEqual(yt.payload, { round: 1, pick: 11, picks_until_your_turn: 0 })
  // Same state next tick → no your_turn, no snake_status.
  const second = detectSnakeEvents(first.next, curr)
  assert.equal(second.events.filter((e) => e.type === 'your_turn').length, 0)
  assert.equal(second.events.filter((e) => e.type === 'snake_status').length, 0)
})

test('events: your_turn_soon fires once at exactly 2 picks away', () => {
  const curr = resolveSnakeState(rootFor('snake-postpick.html')) // up in 2
  const start = { ...initSnakeMemory(), lastPicksUntil: 3 }
  const r = detectSnakeEvents(start, curr)
  const soon = r.events.find((e) => e.type === 'your_turn_soon')
  assert.ok(soon)
  assert.equal(soon.payload.picks_until_your_turn, 2)
  // Does not refire on the next steady tick.
  assert.equal(
    detectSnakeEvents(r.next, curr).events.filter((e) => e.type === 'your_turn_soon').length,
    0
  )
})

test('events: snake_status carries pick/round/countdown, deduped then re-fires on change', () => {
  const waiting = resolveSnakeState(rootFor('snake-waiting.html'))
  const r1 = detectSnakeEvents(initSnakeMemory(), waiting)
  const s1 = r1.events.find((e) => e.type === 'snake_status')
  assert.deepEqual(s1.payload, {
    current_pick: 3,
    current_round: 1,
    picks_until_your_turn: 8,
  })
  // Steady tick → no snake_status.
  const r2 = detectSnakeEvents(r1.next, waiting)
  assert.equal(r2.events.filter((e) => e.type === 'snake_status').length, 0)
  // Different capture (pick 12, up in 2) → snake_status re-fires.
  const postpick = resolveSnakeState(rootFor('snake-postpick.html'))
  const r3 = detectSnakeEvents(r2.next, postpick)
  assert.ok(r3.events.find((e) => e.type === 'snake_status'))
})

test('events: snake_pick emits the just-completed pick, deduped by pick number', () => {
  const curr = resolveSnakeState(rootFor('snake-onclock.html'))
  const r = detectSnakeEvents(initSnakeMemory(), curr)
  const pick = r.events.find((e) => e.type === 'snake_pick')
  assert.ok(pick, 'snake_pick fires for the just-completed pick')
  assert.deepEqual(pick.payload, {
    pick_number: 10,
    player_name: 'C. Brown',
    position: 'RB',
    nfl_team: 'Cin',
    picker: 'Koby',
    is_yours: false,
    round: 1,
  })
  // Same pick number next tick → not re-emitted.
  assert.equal(
    detectSnakeEvents(r.next, curr).events.filter((e) => e.type === 'snake_pick').length,
    0
  )
})

test('events: YOUR snake_pick carries is_yours=true', () => {
  const curr = resolveSnakeState(rootFor('snake-postpick.html'))
  const pick = detectSnakeEvents(initSnakeMemory(), curr).events.find(
    (e) => e.type === 'snake_pick'
  )
  assert.equal(pick.payload.is_yours, true)
  assert.equal(pick.payload.player_name, 'C. Lamb')
})

// ---------------------------------------------------------------------------
// CROSS-POLLER GUARD — content-only, both directions, in the SAME pass:
// snake activates on its turn banner; auction on a Proj-$ nominee / $-budget
// cards. The shared React root is NOT a discriminator (hasAuctionRoot retired).
// ---------------------------------------------------------------------------
test('guard: snake ACTIVE on every snake fixture, INERT on every auction fixture', () => {
  for (const f of ['snake-onclock.html', 'snake-waiting.html', 'snake-postpick.html']) {
    assert.equal(shouldSnakeActivate(rootFor(f)), true, `snake active on ${f}`)
    assert.equal(hasSnakeContent(rootFor(f)), true, `snake content on ${f}`)
  }
  for (const f of ['lobby.html', 'nomination.html', 'your-turn.html', 'post-pick.html']) {
    assert.equal(shouldSnakeActivate(rootFor(f)), false, `snake inert on ${f}`)
  }
})

test('guard: SAME-PASS — auction-active vs lobby AND auction-inert vs a snake fixture', () => {
  // The exact regression this guard exists to prevent: a budget-less 180-cell
  // snake board must NOT trip the auction poller, while a real auction lobby must.
  assert.equal(shouldAuctionActivate(docFor('lobby.html')), true)
  assert.equal(shouldAuctionActivate(docFor('snake-onclock.html')), false)
  assert.equal(shouldAuctionActivate(docFor('snake-waiting.html')), false)
  assert.equal(shouldAuctionActivate(docFor('snake-postpick.html')), false)
})

// ---------------------------------------------------------------------------
// Content-script wiring — non-destructive (no Picks-tab click), content-gated.
// ---------------------------------------------------------------------------
test('snake content script reads the React board, never clicks a tab', () => {
  // Reads the shared React root via the resolver.
  assert.match(snakeSrc, /resolveSnakeState\(/)
  assert.match(snakeSrc, /snakeRoot\(document\)/)
  // The old destructive Picks-tab click + #app innerText scan are GONE.
  assert.doesNotMatch(snakeSrc, /clickPicksTab/)
  assert.doesNotMatch(snakeSrc, /querySelector\('#app'\)/)
  assert.doesNotMatch(snakeSrc, /\.click\(\)/)
})

test('snake content script gates activation on positive snake content', () => {
  assert.match(snakeSrc, /shouldSnakeActivate\(snakeRoot\(document\)\)/)
  assert.match(snakeSrc, /function bootstrap\(\)/)
  // No auction-root veto anywhere (hasAuctionRoot retired).
  assert.doesNotMatch(snakeSrc, /hasAuctionRoot/)
  assert.doesNotMatch(snakeSrc, /AUCTION_ROOT_SELECTOR/)
})
