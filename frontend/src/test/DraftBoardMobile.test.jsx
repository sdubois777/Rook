/**
 * The DraftBoard's mobile column set.
 *
 * The board used to hide the number it exists to produce on phones: `ai_bid_ceiling`
 * (auction) and `adp_rank` (snake) were both behind `hidden sm:block`, so below 640px a
 * user saw a name, a gap and some badges — but not our price or our pick position.
 *
 * IMPORTANT: jsdom does NOT evaluate media queries, so `sm:`/`md:`/`lg:` variants are
 * inert here and computed visibility proves nothing. Everything below asserts on the
 * className string instead: a bare `hidden` means hidden at the base (phone) tier, and a
 * `sm:w-*` proves the desktop width survived. That is the honest limit of what jsdom can
 * pin — the pixel-equivalence claim at >=1024px is only provable in a real browser.
 */
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LeagueContext } from '../context/LeagueContext'
import DraftBoard from '../pages/DraftBoard'

vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: true, getToken: async () => 'tok' }),
}))

vi.mock('../api/draftboard', () => ({
  fetchDraftboard: vi.fn().mockResolvedValue({
    tiers: {
      1: [{
        id: 'p1', name: 'Bijan Robinson', position: 'RB', team_abbr: 'ATL', tier: 1,
        ai_bid_ceiling: 188, market_value: 50, recommended_bid_ceiling: 188, ppr_points: 300,
        adp_ai: 3.0, adp_fantasypros: 5, adp_scoring: 'ppr', value_assessment: 'good_value',
        adp_rank: 1, adp_diff: -4, snake_flag: 'TARGET', value_gap: 138, round_num: 1,
      }],
    },
    total_players: 1,
  }),
}))

function renderBoard(isSnake) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const value = {
    isSnake,
    isAuction: !isSnake,
    draftType: isSnake ? 'snake' : 'auction',
    scoringFormat: 'ppr',
    selectedLeague: { draft_type: isSnake ? 'snake' : 'auction', team_count: 12 },
    setSelectedLeague() {},
    formatOverride: null,
    setFormatOverride() {},
    canChooseFormat: false,
  }
  return render(
    <QueryClientProvider client={qc}>
      <LeagueContext.Provider value={value}>
        <DraftBoard />
      </LeagueContext.Provider>
    </QueryClientProvider>
  )
}

// A bare `hidden` (not `sm:hidden` etc.) is what hides an element on phones.
function hiddenAtBase(el) {
  return /(^|\s)hidden(\s|$)/.test(el.className)
}

describe('DraftBoard mobile — our own number is visible on phones', () => {
  it('snake: the AI ADP cell is visible at base and keeps its desktop width', async () => {
    renderBoard(true)
    const cell = await screen.findByText('1')            // adp_rank via formatAdp
    expect(hiddenAtBase(cell)).toBe(false)
    expect(cell.className).toMatch(/sm:w-20/)            // desktop width preserved
  })

  it('auction: the AI Ceil cell is visible at base and keeps its desktop width', async () => {
    renderBoard(false)
    const cell = await screen.findByText('$188')         // ai_bid_ceiling via getBidCeiling
    expect(hiddenAtBase(cell)).toBe(false)
    expect(cell.className).toMatch(/sm:w-20/)
  })

  it('snake: Diff and the snake flag stay visible — the CLAUDE.md always-on mandate', async () => {
    renderBoard(true)
    const diff = await screen.findByText('-4')
    expect(hiddenAtBase(diff)).toBe(false)
    const flag = screen.getByText('TARGET')
    expect(hiddenAtBase(flag)).toBe(false)
  })

  it('auction: Gap stays visible', async () => {
    renderBoard(false)
    const gap = await screen.findByText('+138')
    expect(hiddenAtBase(gap)).toBe(false)
  })

  it('consensus columns still give way on phones (nothing else regressed)', async () => {
    renderBoard(false)
    // Market ($50) is md+, Points (300) is lg+ — both duplicate/raw info.
    expect(hiddenAtBase(await screen.findByText('$50'))).toBe(true)
    expect(screen.getByText('$50').className).toMatch(/md:block/)
    expect(hiddenAtBase(screen.getByText('300'))).toBe(true)
    expect(screen.getByText('300').className).toMatch(/lg:block/)
  })
})

describe('DraftBoard mobile — header and cell widths move in lockstep', () => {
  // The header is a SEPARATE flex row from the data row, so a width class present on one
  // and not the other misaligns every column to its right — on phones only, which is
  // where nobody looks. This is the test for that class of bug.
  const WIDTHS = [/\bw-10\b/, /\bw-12\b/, /\bw-16\b/, /\bw-20\b/,
                  /\bsm:w-16\b/, /\bsm:w-20\b/]

  // The width lives on different nodes depending on the column: on the wrapper <span> for
  // AI ADP / AI Ceil, and on the <button> itself for Diff / Gap (passed via className).
  // Walking up to the nearest ancestor that carries ANY width class keeps the assertion
  // about the thing that matters rather than about the DOM shape.
  function widthSig(el) {
    let node = el
    while (node && node !== document.body) {
      const sig = WIDTHS.filter((w) => w.test(node.className || '')).map(String)
      if (sig.length) return sig.join(',')
      node = node.parentElement
    }
    return ''
  }

  it('snake: AI ADP and Diff headers match their cells', async () => {
    renderBoard(true)
    const adpCell = await screen.findByText('1')
    const adpSig = widthSig(screen.getAllByText('AI ADP')[0])
    expect(adpSig).not.toBe('')                       // the walk found something
    expect(adpSig).toBe(widthSig(adpCell))

    const diffCell = screen.getByText('-4')
    expect(widthSig(screen.getAllByText('Diff')[0])).toBe(widthSig(diffCell))
  })

  it('auction: AI Ceil and Gap headers match their cells', async () => {
    renderBoard(false)
    const ceilCell = await screen.findByText('$188')
    const ceilSig = widthSig(screen.getAllByText('AI Ceil')[0])
    expect(ceilSig).not.toBe('')
    expect(ceilSig).toBe(widthSig(ceilCell))

    const gapCell = screen.getByText('+138')
    expect(widthSig(screen.getAllByText('Gap')[0])).toBe(widthSig(gapCell))
  })

  it('the short phone-tier header label is rendered alongside the full one', async () => {
    renderBoard(false)
    await screen.findByText('$188')                   // wait out the query
    // shortLabel swaps at sm, so BOTH strings exist in the DOM; jsdom can only prove the
    // pair is present and correctly gated.
    expect(screen.getAllByText('AI $')[0].className).toMatch(/sm:hidden/)
    expect(screen.getAllByText('AI Ceil')[0].className).toMatch(/hidden sm:inline/)
  })
})
