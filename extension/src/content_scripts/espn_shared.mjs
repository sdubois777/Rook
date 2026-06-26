/**
 * Shared ESPN draft-room DOM helpers (React + styled-jsx / Next.js).
 *
 * ESPN has no stable root id, so we GATE ON CONTENT, never on a root. Anchor
 * policy (mirrors the Yahoo `_ys_*` discipline):
 *   - `data-testid` + hand-authored semantic classes (`.clock__digits`,
 *     `.draft-board-grid-header-cell`, `.completedPick`, …) = PRIMARY.
 *   - `jsx-<digits>` (styled-jsx, rotates per deploy) = FALLBACK ONLY, behind a
 *     text/structure check, with `console.warn` + `selector_health='fallback'`.
 *
 * The board structure (column→team headers + grid-positioned pick cells) is
 * common to both formats, so it lives here. Pure DOM-structural (takes a
 * document/root) and free of network side effects → unit-testable by parsing
 * captured ESPN outerHTML with linkedom (test/fixtures/espn/).
 */

export const ESPN_DRAFT_URL_RE = /^https:\/\/fantasy\.espn\.com\/football\/draft/

export const txt = (el) => (el && el.textContent ? el.textContent.trim() : '')

/** First integer in a string (e.g. "$53" → 53, "PICK 12" → 12). null if none. */
export function num(s) {
  const m = (s || '').match(/-?\d+/)
  return m ? parseInt(m[0], 10) : null
}

/** ESPN team labels are prefixed "N. " in the pick train — strip it. */
export function cleanTeamName(s) {
  return (s || '').replace(/^\s*\d+\.\s*/, '').trim()
}

/**
 * DarkReader (a browser extension) injects `data-darkreader-*` attributes on the
 * live page. They never touch our testid/semantic anchors, but strip them so a
 * captured fixture and a DarkReader user's live DOM resolve identically. No-op on
 * clean captures.
 */
export function stripDarkreader(root) {
  if (!root || !root.querySelectorAll) return root
  for (const el of root.querySelectorAll('[data-darkreader-inline-bgcolor], [data-darkreader-inline-color], [data-darkreader-inline-border-top], *')) {
    if (!el.attributes) continue
    for (const a of Array.from(el.attributes)) {
      if (a.name.startsWith('data-darkreader-')) el.removeAttribute(a.name)
    }
  }
  return root
}

// ---------------------------------------------------------------------------
// Shared spine: clock + pick counter
// ---------------------------------------------------------------------------
/**
 * `[data-testid="clock"]` → `.clock__label` + `.clock__digits`.
 *   - Salary cap label: "PK {n} OF {total}". Between picks digits show "--:--".
 *   - Snake label: "RND {r} of {R}".
 * Returns { label, digits, seconds, pickNum, pickTotal, round, roundTotal }.
 */
export function resolveClock(root) {
  const clock = root && root.querySelector('[data-testid="clock"]')
  const label = clock ? txt(clock.querySelector('.clock__label')) : ''
  const digits = clock ? txt(clock.querySelector('.clock__digits')) : ''
  const sec = /^\d{1,2}:\d{2}$/.test(digits)
    ? (() => {
        const [m, s] = digits.split(':').map((x) => parseInt(x, 10))
        return m * 60 + s
      })()
    : null
  const pk = label.match(/PK\s+(\d+)\s+OF\s+(\d+)/i)
  const rnd = label.match(/RND\s+(\d+)\s+of\s+(\d+)/i)
  return {
    label,
    digits,
    seconds: sec,
    pickNum: pk ? parseInt(pk[1], 10) : null,
    pickTotal: pk ? parseInt(pk[2], 10) : null,
    round: rnd ? parseInt(rnd[1], 10) : null,
    roundTotal: rnd ? parseInt(rnd[2], 10) : null,
  }
}

// ---------------------------------------------------------------------------
// Board: column→team headers + grid-positioned completed picks
// ---------------------------------------------------------------------------
/**
 * `.draft-board-grid-header-cell` × N (column order = DOM order). The header cell
 * itself carries `.myTeam` (the viewer's own column) and `.onTheClock` (the
 * currently-picking column). Returns [{ col, team, isMine, onClock }] (col is
 * 1-based to match the cells' inline `grid-area: row / col`).
 */
export function resolveBoardHeaders(root) {
  const cells = root ? Array.from(root.querySelectorAll('.draft-board-grid-header-cell')) : []
  return cells.map((c, i) => ({
    col: i + 1,
    team: txt(c),
    isMine: c.classList.contains('myTeam'),
    onClock: c.classList.contains('onTheClock'),
  }))
}

/** The viewer's own team name from the board (`.myTeam` header). null if absent. */
export function resolveMyTeam(root) {
  const me = (resolveBoardHeaders(root) || []).find((h) => h.isMine)
  return me ? me.team : null
}

/** Parse "grid-area: {row} / {col}" off an element's inline style. */
export function gridArea(el) {
  const m = (el && el.getAttribute('style') || '').match(/grid-area:\s*(\d+)\s*\/\s*(\d+)/)
  return m ? { row: parseInt(m[1], 10), col: parseInt(m[2], 10) } : { row: null, col: null }
}

/**
 * Completed board picks → [{ row, col, team, isMine, roundPick, round,
 * pickInRound, name, firstName, lastName, proTeam, position, byeWeek,
 * winningPrice }]. `headers` (from resolveBoardHeaders) maps column → drafting
 * team. Snake cells carry `.roundPick`/`.byeWeek`; salary-cap cells carry
 * `.winningPrice`/`.rosterSlot` (no `.roundPick`).
 */
export function resolveCompletedPicks(root, headers) {
  const byCol = new Map((headers || []).map((h) => [h.col, h]))
  const cells = root ? Array.from(root.querySelectorAll('.completedPick')) : []
  return cells.map((c) => {
    const { row, col } = gridArea(c)
    const head = byCol.get(col)
    const first = txt(c.querySelector('.playerFirstName'))
    const last = txt(c.querySelector('.playerLastName'))
    const rp = txt(c.querySelector('.roundPick')) // "1.1" = round.pickInRound (snake)
    const rpm = rp.match(/(\d+)\.(\d+)/)
    const priceTxt = txt(c.querySelector('.winningPrice')) // salary cap "$65"
    return {
      row,
      col,
      team: head ? head.team : null,
      isMine: head ? head.isMine : false,
      roundPick: rp || null,
      round: rpm ? parseInt(rpm[1], 10) : null,
      pickInRound: rpm ? parseInt(rpm[2], 10) : null,
      firstName: first,
      lastName: last,
      name: [first, last].filter(Boolean).join(' ').trim() || null,
      proTeam: txt(c.querySelector('.playerProTeam')) || null,
      position: txt(c.querySelector('.positionPill')) || null,
      byeWeek: (txt(c.querySelector('.byeWeek')).match(/\d+/) || [null])[0],
      winningPrice: priceTxt ? num(priceTxt) : null,
    }
  })
}
