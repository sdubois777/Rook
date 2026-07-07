import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseHTML } from 'linkedom'
import { resolveRosterSlots as espnSlots } from '../src/content_scripts/espn_shared.mjs'
import { resolveRosterSlots as yahooSlots } from '../src/content_scripts/yahoo_auction_resolve.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIX = join(__dirname, 'fixtures')
const dom = (p) => parseHTML(readFileSync(join(FIX, p), 'utf8')).document

// ESPN — div[title="Position"], every slot labeled always (auction + snake).
test('ESPN resolveRosterSlots: full template from the real salarycap fixture', () => {
  const tokens = espnSlots(dom('espn/salarycap/board-mid.html'))
  assert.deepEqual(tokens.slice(0, 9), ['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'FLEX', 'D/ST', 'K'])
  assert.equal(tokens.filter((t) => t === 'BE').length, 7)
  assert.equal(tokens.length, 16)
})

test('ESPN resolveRosterSlots: snake fixture has the identical template', () => {
  const tokens = espnSlots(dom('espn/snake/board-mid.html'))
  assert.deepEqual(tokens.slice(0, 9), ['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'FLEX', 'D/ST', 'K'])
})

// Yahoo — concatenated badge spans; the flex is "WRT", NOT a phantom "W".
test('Yahoo resolveRosterSlots: pre-draft grid, flex correctly concatenated', () => {
  const { tokens, total } = yahooSlots(dom('auction/lobby.html'))
  assert.equal(total, 15) // n/15 checksum denominator
  assert.ok(tokens.includes('WRT'), 'flex badge concatenated to WRT, not a phantom W')
  assert.ok(!tokens.includes('W'), 'no phantom single-letter W')
  // exactly one team's 15 slots
  const counts = tokens.reduce((m, t) => ((m[t] = (m[t] || 0) + 1), m), {})
  assert.deepEqual(counts, { QB: 1, WR: 2, RB: 2, TE: 1, WRT: 1, K: 1, DEF: 1, BN: 6 })
})
