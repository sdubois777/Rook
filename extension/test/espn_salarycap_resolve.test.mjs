import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseHTML } from 'linkedom'

import {
  isSalaryCap,
  resolveTeams,
  resolveNominee,
  resolveBidding,
  resolveSalaryCapState,
  detectSalaryCapEvents,
  initSalaryCapMemory,
} from '../src/content_scripts/espn_salarycap_resolve.mjs'
import { isSnake } from '../src/content_scripts/espn_snake_resolve.mjs'
import { stripDarkreader } from '../src/content_scripts/espn_shared.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIX = join(__dirname, 'fixtures', 'espn', 'salarycap')

// Fixture loader: sanitize DarkReader noise (matches the runtime sanitizer) so a
// capture and a DarkReader user's live DOM resolve identically.
function docFor(name) {
  const { document } = parseHTML(readFileSync(join(FIX, name), 'utf-8'))
  stripDarkreader(document)
  return document
}
const MY_TEAM = "Stephen's Smart Team"

// ---------------------------------------------------------------------------
// Gate
// ---------------------------------------------------------------------------
test('gate: salary cap active on every salary-cap fixture; snake gate inert', () => {
  for (const f of ['lobby', 'nomination-active', 'nomination-bid', 'sale', 'board-mid', 'complete']) {
    const d = docFor(`${f}.html`)
    assert.equal(isSalaryCap(d), true, `salaryCap active on ${f}`)
    assert.equal(isSnake(d), false, `snake inert on salary-cap ${f}`)
  }
})

// ---------------------------------------------------------------------------
// Teams (the pick train) — 12 cards, budgets, self + selecting markers
// ---------------------------------------------------------------------------
test('teams: 12 cards with budget/bid/own/selecting off stable anchors', () => {
  const { teams, myTeam, selectingTeam } = resolveTeams(docFor('nomination-active.html'))
  assert.equal(Object.keys(teams).length, 12)
  assert.equal(myTeam, MY_TEAM)
  assert.equal(selectingTeam, 'Team 4') // the --selecting cell (nominator)
  // names are stripped of the "N. " prefix
  assert.ok(teams[MY_TEAM], 'own team keyed by clean name')
  assert.equal(teams[MY_TEAM].isMine, true)
  // $-budget parsed; "$null" bid → null
  assert.equal(teams['Team 5'].budget, 200)
  assert.equal(teams['Team 5'].bid, null)
  // the live high bid sits on exactly one card
  assert.equal(teams['Team 6'].bid, 53)
})

// ---------------------------------------------------------------------------
// Nominee + bidding (the previously-missing state)
// ---------------------------------------------------------------------------
test('nominee: full name + pro team + position (name-only, no id)', () => {
  const n = resolveNominee(docFor('nomination-active.html'))
  assert.deepEqual(n, { name: 'Bijan Robinson', proTeam: 'ATL', position: 'RB' })
})

test('nominee: null when no player is on the block (lobby)', () => {
  assert.equal(resolveNominee(docFor('lobby.html')), null)
})

test('bidding: current offer + my remaining max', () => {
  assert.deepEqual(resolveBidding(docFor('nomination-active.html')), { currentBid: 53, maxBid: 117 })
})

test('high bidder = the team whose live bid equals the current offer', () => {
  const st = resolveSalaryCapState(docFor('nomination-active.html'))
  assert.equal(st.currentBid, 53)
  assert.equal(st.highBidder, 'Team 6')
})

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
test('nomination event: name + pos_team + opening bid + high bidder', () => {
  const st = resolveSalaryCapState(docFor('nomination-active.html'))
  const ev = detectSalaryCapEvents(initSalaryCapMemory(), st).events.find((e) => e.type === 'nomination')
  assert.ok(ev, 'nomination emitted')
  assert.equal(ev.payload.player_name, 'Bijan Robinson')
  assert.equal(ev.payload.pos_team, 'RB · ATL')
  assert.equal(ev.payload.opening_bid, 53)
  assert.equal(ev.payload.current_bidder, 'Team 6')
})

test('bid_update: same nominee, raised bid + high-bidder change', () => {
  const st = resolveSalaryCapState(docFor('nomination-active.html'))
  // prior tick: same nominee on the block at a lower bid by a different team.
  const prev = { ...initSalaryCapMemory(), lastNominee: 'Bijan Robinson', lastBid: 40, lastBidder: 'Team 9' }
  const ev = detectSalaryCapEvents(prev, st).events.find((e) => e.type === 'bid_update')
  assert.ok(ev, 'bid_update emitted')
  assert.equal(ev.payload.current_bid, 53)
  assert.equal(ev.payload.current_bidder, 'Team 6')
  // and NOT re-emitted as a fresh nomination
  assert.equal(detectSalaryCapEvents(prev, st).events.some((e) => e.type === 'nomination'), false)
})

test('clock event: fires on a 5s cadence while a nominee is up', () => {
  const base = resolveSalaryCapState(docFor('nomination-active.html'))
  const st = { ...base, clock: { ...base.clock, digits: '00:15', seconds: 15 } }
  const prev = { ...initSalaryCapMemory(), lastNominee: 'Bijan Robinson', lastBid: 53, lastBidder: 'Team 6' }
  const ev = detectSalaryCapEvents(prev, st).events.find((e) => e.type === 'clock')
  assert.ok(ev)
  assert.equal(ev.payload.seconds_remaining, 15)
  // off-cadence second does not emit
  const off = { ...base, clock: { ...base.clock, digits: '00:14', seconds: 14 } }
  assert.equal(detectSalaryCapEvents(prev, off).events.some((e) => e.type === 'clock'), false)
})

test('draft_pick (sale): board-delta picks → winner via column header + price', () => {
  const st = resolveSalaryCapState(docFor('sale.html'))
  const picks = detectSalaryCapEvents(initSalaryCapMemory(), st).events.filter((e) => e.type === 'draft_pick')
  assert.equal(picks.length, 4)
  const chase = picks.find((p) => p.payload.player_name === "Ja'Marr Chase")
  assert.equal(chase.payload.final_price, 65)
  assert.equal(chase.payload.winner, 'Team 12')
  assert.equal(chase.payload.is_yours, false)
  // already-seen cells are not re-emitted
  const { next } = detectSalaryCapEvents(initSalaryCapMemory(), st)
  assert.equal(detectSalaryCapEvents(next, st).events.filter((e) => e.type === 'draft_pick').length, 0)
})

test('teams_update: 12 teams + your_team_id', () => {
  const st = resolveSalaryCapState(docFor('nomination-active.html'))
  const ev = detectSalaryCapEvents(initSalaryCapMemory(), st).events.find((e) => e.type === 'teams_update')
  assert.ok(ev)
  assert.equal(Object.keys(ev.payload.teams).length, 12)
  assert.equal(ev.payload.your_team_id, MY_TEAM)
})

test('sanitizer strips data-darkreader-* (defensive; fixtures are already clean)', () => {
  const { document } = parseHTML('<div data-darkreader-inline-color="x" class="completedPick">y</div>')
  stripDarkreader(document)
  assert.equal(document.querySelector('.completedPick').hasAttribute('data-darkreader-inline-color'), false)
})
