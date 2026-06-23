import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  parseDraftState,
  parseTeamLine,
  detectWinner,
  detectEvents,
  secondsFromClock,
} from '../src/content_scripts/yahoo_draft_parse.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const auctionSrc = readFileSync(
  join(__dirname, '..', 'src', 'content_scripts', 'yahoo_draft.js'),
  'utf-8'
)

// A representative #draft innerText snapshot mid-nomination.
const SAMPLE = [
  'Sam LaPorta',
  'DET – TE',
  '$4',
  '0:19 Remaining',
  'Stephen$96/15',
  'Team 5$108/15',
  'Team 3$1490/15',
].join('\n')

test('parseDraftState extracts player name', () => {
  const s = parseDraftState(SAMPLE)
  assert.equal(s.playerName, 'Sam LaPorta')
  assert.equal(s.posTeam, 'DET – TE')
})

test('parseDraftState extracts current bid', () => {
  assert.equal(parseDraftState(SAMPLE).currentBid, 4)
})

test('parseDraftState extracts clock', () => {
  assert.equal(parseDraftState(SAMPLE).clock, '0:19')
})

test('parseDraftState returns null player between nominations', () => {
  const waiting = ['Stephen$96/15', 'Team 5$108/15'].join('\n')
  const s = parseDraftState(waiting)
  assert.equal(s.playerName, null)
  assert.equal(Object.keys(s.teams).length, 2)
})

test('parseTeamLine handles single digit budget', () => {
  // "Stephen$96/15" => budget $9, slots 6, total 15
  assert.deepEqual(parseTeamLine('Stephen$96/15'), {
    name: 'Stephen',
    budget: 9,
    slotsUsed: 6,
    totalSlots: 15,
  })
})

test('parseTeamLine handles triple digit budget', () => {
  // "Team 3$1490/15" => budget $149, slots 0, total 15
  assert.deepEqual(parseTeamLine('Team 3$1490/15'), {
    name: 'Team 3',
    budget: 149,
    slotsUsed: 0,
    totalSlots: 15,
  })
})

test('secondsFromClock parses M:SS', () => {
  assert.equal(secondsFromClock('0:19'), 19)
  assert.equal(secondsFromClock('1:05'), 65)
  assert.equal(secondsFromClock(null), null)
})

test('detectWinner finds team by budget delta', () => {
  const prev = { Stephen: { budget: 96, slotsUsed: 5, totalSlots: 15 } }
  const current = { Stephen: { budget: 86, slotsUsed: 6, totalSlots: 15 } }
  assert.equal(detectWinner(current, prev, 10), 'Stephen')
})

test('detectWinner returns null when no delta matches', () => {
  const prev = { Stephen: { budget: 96, slotsUsed: 5, totalSlots: 15 } }
  const current = { Stephen: { budget: 96, slotsUsed: 5, totalSlots: 15 } }
  assert.equal(detectWinner(current, prev, 10), null)
})

const EMPTY_MEM = { lastPlayer: null, lastBid: null, lastClock: null, prevTeams: {} }

test('nomination event fires on player change', () => {
  const curr = parseDraftState(SAMPLE)
  const { events, next } = detectEvents(EMPTY_MEM, curr)
  const nom = events.find((e) => e.type === 'nomination')
  assert.ok(nom, 'nomination event emitted')
  assert.equal(nom.payload.player_name, 'Sam LaPorta')
  assert.equal(nom.payload.opening_bid, 4)
  assert.equal(next.lastPlayer, 'Sam LaPorta')
})

test('bid_update fires on same player bid change', () => {
  const mem = { lastPlayer: 'Sam LaPorta', lastBid: 4, lastClock: '0:19', prevTeams: {} }
  const curr = {
    playerName: 'Sam LaPorta',
    posTeam: 'DET – TE',
    currentBid: 6,
    clock: '0:18',
    teams: {},
  }
  const { events } = detectEvents(mem, curr)
  assert.equal(events.length, 1)
  assert.equal(events[0].type, 'bid_update')
  assert.equal(events[0].payload.current_bid, 6)
})

test('sold event fires when player goes null', () => {
  const mem = {
    lastPlayer: 'Sam LaPorta',
    lastBid: 6,
    lastClock: '0:01',
    prevTeams: { Stephen: { budget: 96, slotsUsed: 5, totalSlots: 15 } },
  }
  const curr = {
    playerName: null,
    posTeam: null,
    currentBid: null,
    clock: null,
    teams: { Stephen: { budget: 90, slotsUsed: 6, totalSlots: 15 } },
  }
  const { events, next } = detectEvents(mem, curr)
  const sold = events.find((e) => e.type === 'draft_pick')
  assert.ok(sold, 'draft_pick event emitted')
  assert.equal(sold.payload.player_name, 'Sam LaPorta')
  assert.equal(sold.payload.final_price, 6)
  assert.equal(sold.payload.winner, 'Stephen')
  assert.equal(next.lastPlayer, null)
})

// ---------------------------------------------------------------------------
// Cross-poller guard (static): the auction poller gates startup on the React
// resolver's shouldAuctionActivate (gate logic itself is unit-tested in
// yahoo_auction_resolve.test.mjs). The old `#draft`-presence gate was removed
// with Yahoo's React replatform.
// ---------------------------------------------------------------------------

test('auction content script gates startup on shouldAuctionActivate', () => {
  assert.ok(
    auctionSrc.includes('shouldAuctionActivate'),
    'yahoo_draft.js must gate startup on shouldAuctionActivate'
  )
})
