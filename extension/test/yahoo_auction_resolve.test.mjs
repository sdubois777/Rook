import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseHTML } from 'linkedom'

import {
  auctionGateDecision,
  isNameShape,
  resolveAuctionState,
  shouldAuctionActivate,
  isDraftComplete,
  detectAuctionEvents,
  initAuctionMemory,
} from '../src/content_scripts/yahoo_auction_resolve.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIX = join(__dirname, 'fixtures', 'auction')

// Parse a captured Yahoo outerHTML fixture into a document (+ React root).
function docFor(name) {
  const { document } = parseHTML(readFileSync(join(FIX, name), 'utf-8'))
  return document
}
function rootFor(name) {
  return docFor(name).querySelector('#main-0-DraftClientBootstrap-Proxy')
}

// ---------------------------------------------------------------------------
// Activation gate — pure decision (no DOM). Root + live signal + cross-poller
// veto + draft-complete negative override.
// ---------------------------------------------------------------------------
const GATE = {
  hasRoot: true,
  hasLiveTimer: false,
  teamCardCount: 0,
  draftComplete: false,
  snakeMarkers: false,
}

test('gate: ACTIVE with root + a live timer', () => {
  assert.equal(auctionGateDecision({ ...GATE, hasLiveTimer: true }), true)
})

test('gate: ACTIVE with root + >=1 .ys-team card (lobby, no timer yet)', () => {
  assert.equal(auctionGateDecision({ ...GATE, teamCardCount: 12 }), true)
})

test('gate: INERT without the React root (even with a timer + cards)', () => {
  assert.equal(
    auctionGateDecision({ ...GATE, hasRoot: false, hasLiveTimer: true, teamCardCount: 12 }),
    false
  )
})

test('gate: INERT when draft-complete (negative override)', () => {
  assert.equal(
    auctionGateDecision({ ...GATE, teamCardCount: 12, draftComplete: true }),
    false
  )
})

test('gate: INERT when snake markers present (cross-poller veto)', () => {
  assert.equal(
    auctionGateDecision({ ...GATE, hasLiveTimer: true, teamCardCount: 12, snakeMarkers: true }),
    false
  )
})

test('gate: INERT with the root but no live signal (root only)', () => {
  assert.equal(auctionGateDecision(GATE), false)
})

// ---------------------------------------------------------------------------
// Name-shape gate (deterministic, no DOM) — keeps flicker/garbage from firing a
// nomination (which on-change diffing would otherwise spam as new nominations).
// ---------------------------------------------------------------------------
test('isNameShape: accepts a >=2-token capitalized name', () => {
  assert.equal(isNameShape('Bijan Robinson'), true)
  assert.equal(isNameShape('A. Jeanty'), true)
})

test('isNameShape: rejects money / number / roster / clock / label / single token', () => {
  for (const t of ['$45', '12', '1/15', '00:19', 'You', 'Current Bid', 'Saquon', ''])
    assert.equal(isNameShape(t), false, `should reject: ${JSON.stringify(t)}`)
})

// ---------------------------------------------------------------------------
// Fixture-backed resolver tests — REAL captured Yahoo outerHTML (the regression
// net; re-runnable after each Yahoo deploy). Do NOT hand-mock the markup.
// Remaining states are SKIPPED until those captures land.
// ---------------------------------------------------------------------------

// nomination.html — a real mid-nomination capture: T. McMillan (id 41793) on the
// block, $1 bid by Team 3, clock 00:19, 4 nominations until our turn, our team
// (data-id 4) is "You" with $200 / 0-15. All fields resolve off stable anchors
// (ys-player[data-id] name, structural offer-panel bid/bidder, .ys-team teams).
test('nomination: resolves name/bid/clock/bidder/teams off stable anchors', () => {
  const doc = docFor('nomination.html')
  const root = doc.querySelector('#main-0-DraftClientBootstrap-Proxy')
  const warns = []
  const st = resolveAuctionState(root, { warn: (f, l) => warns.push(`${f}:${l}`) })

  // gate active
  assert.equal(shouldAuctionActivate(doc), true)
  // nominee — id-anchored (Amendment B)
  assert.equal(st.playerName, 'T. McMillan')
  assert.equal(st.playerId, '41793')
  assert.equal(st.posTeam, 'WR · Car')
  // bid + high bidder (structural, cross-checked to the stable team data-id)
  assert.equal(st.currentBid, 1)
  assert.equal(st.currentBidder, 'Team 3')
  assert.equal(st.currentBidderTeamId, '3')
  // clock
  assert.equal(st.clock, '00:19')
  // viewer countdown (full-match anchor, not the catch-all blob's "194")
  assert.equal(st.picksUntilYourTurn, 4)
  // every field off its PRIMARY anchor — no _ys_ fallback, no warns
  assert.deepEqual(st.health, {
    clock: 'primary', name: 'primary', bid: 'primary',
    bidder: 'primary', teams: 'primary', turn: 'primary',
  })
  assert.deepEqual(warns, [])
})

test('nomination: teams + your-team self-id (You span + data-id, NOT a degradation)', () => {
  const root = rootFor('nomination.html')
  const st = resolveAuctionState(root, { warn: () => {} })
  assert.equal(Object.keys(st.teams).length, 12)
  assert.equal(st.yourTeamId, '4') // the "You" card
  // your own card: keyed "You", full budget, 0/15, data-id 4
  assert.deepEqual(st.teams['You'], {
    budget: 200, slotsUsed: 0, totalSlots: 15, dataId: '4',
  })
  // an opponent card carries budget/roster/data-id
  assert.equal(st.teams['Team 3'].dataId, '3')
})

// lobby.html — a TRUE empty lobby (room loaded, no nominee). Gate is active (the
// room is up) but there is NO nomination, and that's NOT a degradation.
test('lobby: gate ACTIVE but NO nominee; teams + self-id resolve clean', () => {
  const doc = docFor('lobby.html')
  const root = doc.querySelector('#main-0-DraftClientBootstrap-Proxy')
  const warns = []
  const st = resolveAuctionState(root, { warn: (f, l) => warns.push(`${f}:${l}`) })
  assert.equal(shouldAuctionActivate(doc), true) // root + .ys-team
  assert.equal(st.playerName, null) // findNomineeEl → null (no "Proj $" nominee)
  assert.equal(st.currentBid, null)
  assert.equal(st.clock, null)
  assert.equal(st.health.name, 'na') // absence of a nomination is not a miss
  assert.equal(st.health.bid, 'na')
  assert.equal(Object.keys(st.teams).length, 12)
  assert.equal(st.yourTeamId, '2') // this league's "You" card is data-id 2
  assert.deepEqual(warns, [])
})

// your-turn.html — "It's your turn to nominate" (N=0). A SUGGESTED player is
// shown (Proj $, no bid); that must NOT be reported as a nomination.
test('your-turn: N=0 via real wording; suggested player is NOT a nomination', () => {
  const doc = docFor('your-turn.html')
  const root = doc.querySelector('#main-0-DraftClientBootstrap-Proxy')
  const st = resolveAuctionState(root, { warn: () => {} })
  assert.equal(shouldAuctionActivate(doc), true)
  assert.equal(st.picksUntilYourTurn, 0) // your-turn-now
  assert.equal(st.health.turn, 'primary')
  assert.equal(st.playerName, null) // suggested, no bid → not an active nomination
  assert.equal(st.currentBid, null)
})

// post-pick.html — real post-sale team budgets. Drive the team-delta draft_pick
// from a prior tick whose nomination is about to resolve to a winner.
test('post-pick: team-delta draft_pick attributed to the last-known nominee', () => {
  const curr = resolveAuctionState(rootFor('post-pick.html'), { warn: () => {} })
  // Reconstruct the pre-sale baseline by reversing one team's last purchase
  // (the winner spent `price` and gained a roster slot).
  const winner = 'Team 10'
  const w = curr.teams[winner]
  const price = 10
  const nominationTeams = JSON.parse(JSON.stringify(curr.teams))
  nominationTeams[winner] = { ...w, budget: w.budget + price, slotsUsed: w.slotsUsed - 1 }
  const prev = {
    ...initAuctionMemory(),
    lastPlayerKey: '99999',
    lastPlayerName: 'Sold Player',
    lastPlayerId: '99999',
    lastBid: price,
    nominationTeams,
  }
  // The nominee left the block (playerName null) → the sale fires.
  const { events } = detectAuctionEvents(prev, { ...curr, playerName: null })
  const sale = events.find((e) => e.type === 'draft_pick')
  assert.ok(sale, 'draft_pick emitted when the nominee leaves the block')
  assert.equal(sale.payload.winner, winner)
  assert.equal(sale.payload.player_name, 'Sold Player') // last-known nominee
  assert.equal(sale.payload.player_id, '99999')
  assert.equal(sale.payload.final_price, price)
  assert.equal(sale.payload.is_yours, false) // an opponent won
})

test('sale fires even when the winner is undetermined (player must leave the board)', () => {
  // No team-budget delta resolvable → winner "unknown", but the pick STILL fires
  // (every nominated auction player is sold), so the UI removes it.
  const prev = {
    ...initAuctionMemory(),
    lastPlayerKey: '123',
    lastPlayerName: 'Gone Player',
    lastPlayerId: '123',
    lastBid: 5,
    nominationTeams: { 'Team 1': { budget: 100, slotsUsed: 1, totalSlots: 15, dataId: '1' } },
  }
  const curr = { teams: { 'Team 1': { budget: 100, slotsUsed: 1, totalSlots: 15, dataId: '1' } }, health: {} }
  const sale = detectAuctionEvents(prev, curr).events.find((e) => e.type === 'draft_pick')
  assert.ok(sale)
  assert.equal(sale.payload.winner, 'unknown')
  assert.equal(sale.payload.player_name, 'Gone Player')
})

test('sale tags is_yours when YOUR card wins (winner === "You")', () => {
  // Your card's budget drops by the price → detectWinner resolves "You" → is_yours.
  const prev = {
    ...initAuctionMemory(),
    lastPlayerKey: '7',
    lastPlayerName: 'My Guy',
    lastPlayerId: '7',
    lastBid: 20,
    nominationTeams: { You: { budget: 200, slotsUsed: 0, totalSlots: 15, dataId: '4' } },
  }
  const curr = { teams: { You: { budget: 180, slotsUsed: 1, totalSlots: 15, dataId: '4' } }, health: {} }
  const sale = detectAuctionEvents(prev, curr).events.find((e) => e.type === 'draft_pick')
  assert.ok(sale)
  assert.equal(sale.payload.winner, 'You')
  assert.equal(sale.payload.is_yours, true)
})

// draft-complete.html — post-draft summary on the SAME root. It has no .ys-team
// and no timer, so the live-signal gate already keeps it inactive; the marker is
// defense-in-depth.
test('draft-complete: gate INACTIVE + draft-complete marker detected', () => {
  const doc = docFor('draft-complete.html')
  const root = doc.querySelector('#main-0-DraftClientBootstrap-Proxy')
  assert.equal(shouldAuctionActivate(doc), false) // no .ys-team, no timer
  assert.equal(isDraftComplete(root), true) // "Thank you for drafting…"
})
test(
  'degradation: breaking a STRUCTURE/TEXT anchor falls to _ys_ + reports fallback/missing (LOUD)',
  // NOTE: the primary anchors are text/structure/kebab — NOT _ys_ hashes — so
  // mutating the hashes alone does NOT degrade (that's the desired property,
  // verified by nomination.html resolving all-primary with the hashes present).
  // This test must instead break a STRUCTURAL anchor (e.g. strip .ys-team or the
  // nominee's "Proj $" text) and assert the _ys_ fallback fires loudly. Holding
  // until we lock the exact fallback selectors against a 2nd-deploy capture.
  { skip: 'design against a real post-deploy capture (hash-mutation alone is a no-op by design)' },
  () => {}
)
