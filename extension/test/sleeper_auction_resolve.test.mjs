import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseFrame, parseUserId } from '../src/content_scripts/sleeper_resolve.mjs'
import { initAuctionMemory, detectAuctionEvents } from '../src/content_scripts/sleeper_auction_resolve.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRAMES = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'sleeper', 'auction.json'), 'utf-8')
)
const MY_USER = '1373225184038764544' // → slot 3 in this fixture

function replay(frames, myUser) {
  let mem = initAuctionMemory(myUser)
  const events = []
  for (const arr of frames) {
    const { events: evs, next } = detectAuctionEvents(mem, parseFrame(JSON.stringify(arr)))
    mem = next
    events.push(...evs)
  }
  return events
}

test('bid_update: each new offer → bidder slot + amount + sleeper_id', () => {
  const bids = replay(FRAMES, MY_USER).filter((e) => e.type === 'bid_update')
  // 7564: 53/61/69/72 ; 9221: 6/12/17
  assert.deepEqual(
    bids.filter((b) => b.payload.sleeper_player_id === '7564').map((b) => b.payload.current_bid),
    [53, 61, 69, 72]
  )
  const top = bids.find((b) => b.payload.current_bid === 72)
  assert.equal(top.payload.current_bidder, 'Team 3')
})

test('nomination: id-only (name resolved backend-side), opening bid + clock', () => {
  const noms = replay(FRAMES, MY_USER).filter((e) => e.type === 'nomination')
  // only 9221 was nominated within the capture window (7564 was mid-bid on join)
  assert.equal(noms.length, 1)
  const n = noms[0]
  assert.equal(n.payload.sleeper_player_id, '9221')
  assert.equal(n.payload.player_name, null) // <-- the wrinkle: no name in the frame
  assert.equal(n.payload.opening_bid, 1)
  assert.equal(n.payload.nominating_slot, 2)
  assert.equal(n.payload.clock_ends_at, '2026-06-27T21:29:12.208837Z')
})

test('draft_pick (sale): winner via slot, price, is_yours, full name', () => {
  const sales = replay(FRAMES, MY_USER).filter((e) => e.type === 'draft_pick')
  assert.equal(sales.length, 1)
  const s = sales[0].payload
  assert.equal(s.player_name, "Ja'Marr Chase")
  assert.equal(s.sleeper_player_id, '7564')
  assert.equal(s.final_price, 72)
  assert.equal(s.winner, 'Team 3')
  assert.equal(s.is_yours, true) // my slot 3 won
})

test('self-win resolves is_yours when user_id comes JSON-quoted from localStorage', () => {
  // End-to-end of the fix: the real localStorage form is "\"<id>\"". Feeding the
  // RAW quoted value (without unwrapping) would miss draft_order and mis-attribute
  // the win to "Team 3"; parseUserId unwraps it so my slot resolves.
  const quoted = '"1373225184038764544"'
  const sale = replay(FRAMES, parseUserId(quoted)).find((e) => e.type === 'draft_pick')
  assert.equal(sale.payload.is_yours, true)
  // ...and the raw quoted id (the bug) would NOT resolve.
  const broken = replay(FRAMES, quoted).find((e) => e.type === 'draft_pick')
  assert.equal(broken.payload.is_yours, false)
})

test('teams_update: budgets derived (budget − Σ won)', () => {
  const tu = replay(FRAMES, MY_USER).filter((e) => e.type === 'teams_update')
  assert.ok(tu.length >= 1)
  const last = tu[tu.length - 1].payload
  // My own slot is keyed "You" (the frontend folds it into myTeamName → one
  // roster entry, not a phantom "Team 3" beside my name).
  assert.equal(last.your_team_id, 'You')
  assert.equal(last.teams['You'].budget, 128) // 200 − 72
  assert.equal(last.teams['You'].isMine, true)
  assert.equal(last.teams['Team 1'].budget, 200) // untouched
  assert.equal(last.teams['Team 3'], undefined) // not duplicated as Team 3
  assert.equal(Object.keys(last.teams).length, 12) // exactly 12 teams, no phantom
})

test('dedupe: a re-transmitted frame (same frame twice) does not double-emit', () => {
  // Replay the full stream, then re-feed the key frames (a WS retransmit) and
  // assert nothing fires again — sale dedupes by pick_no, nomination by nominee,
  // bid by key.
  let mem = initAuctionMemory(MY_USER)
  for (const arr of FRAMES) mem = detectAuctionEvents(mem, parseFrame(JSON.stringify(arr))).next
  const saleFrame = FRAMES.find((f) => f[3] === 'player_picked')
  const nomFrame = FRAMES.find((f) => f[3] === 'draft_updated_by_nomination')
  const lastBidFrame = [...FRAMES].reverse().find((f) => f[3] === 'new_draft_offer')
  assert.equal(detectAuctionEvents(mem, parseFrame(JSON.stringify(saleFrame))).events.length, 0)
  assert.equal(detectAuctionEvents(mem, parseFrame(JSON.stringify(nomFrame))).events.length, 0)
  assert.equal(detectAuctionEvents(mem, parseFrame(JSON.stringify(lastBidFrame))).events.length, 0)
})
