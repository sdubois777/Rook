/**
 * Player display utilities — the SINGLE source of truth for how player ADP /
 * valuation fields are selected and formatted.
 *
 * Import from here; never read raw player ADP fields directly in components.
 * The recurring bug this prevents: adp_ai is the raw model estimate (a float
 * with heavy ties — Bijan/Gibbs/Chase all 4.0), while adp_rank is the clean
 * 1..N integer the board actually shows. Components kept reaching for adp_ai
 * and displaying inconsistent values. getDisplayAdp() always uses adp_rank.
 *
 * Two object shapes flow through the app:
 *   - PLAYER ROWS  (draftboard / available list): adp_rank, adp_fantasypros,
 *     adp_diff, ai_bid_ceiling, snake_flag
 *   - RECOMMENDATIONS (engine output): adp_rank|adp_ai, adp_fp, adp_diff
 *     (note: adp_fp, NOT adp_fantasypros). The getRec* helpers cover this shape.
 */

// --- Player-row ADP selection (never adp_ai) --------------------------------

/** Display ADP — always the clean adp_rank integer, never the tied adp_ai. */
export function getDisplayAdp(player) {
  return player?.adp_rank ?? null
}

/** FantasyPros consensus ADP (also a rank, integer-valued). */
export function getFpAdp(player) {
  return player?.adp_fantasypros ?? null
}

/** ADP differential (fp_rank − adp_rank). Positive = we rate them earlier. */
export function getAdpDiff(player) {
  return player?.adp_diff ?? null
}

/** Auction bid ceiling. */
export function getBidCeiling(player) {
  return player?.ai_bid_ceiling ?? null
}

/** The mode-appropriate primary value: adp_rank (snake) or ceiling (auction). */
export function getPrimaryValue(player, isSnake) {
  return isSnake ? getDisplayAdp(player) : getBidCeiling(player)
}

// --- Player-row formatting --------------------------------------------------

/** Clean integer string for the display ADP, or '--'. */
export function formatAdp(player) {
  const adp = getDisplayAdp(player)
  return adp != null ? String(adp) : '--'
}

/** FP ADP as an integer string (it's a rank), or '--'. */
export function formatFpAdp(player) {
  const fp = getFpAdp(player)
  return fp != null ? String(Math.round(fp)) : '--'
}

/** Signed integer string for the ADP diff: '+3', '-7', '0', or '--'. */
export function formatAdpDiff(player) {
  const diff = getAdpDiff(player)
  if (diff == null) return '--'
  const rounded = Math.round(diff)
  return rounded > 0 ? `+${rounded}` : String(rounded)
}

// --- Sort comparators -------------------------------------------------------

/** Snake draft order: adp_rank ascending; missing ranks sort last. */
export function snakeSortComparator(a, b) {
  return (a?.adp_rank ?? 9999) - (b?.adp_rank ?? 9999)
}

/** Auction draft order: ai_bid_ceiling descending; missing ceilings last. */
export function auctionSortComparator(a, b) {
  return (b?.ai_bid_ceiling ?? 0) - (a?.ai_bid_ceiling ?? 0)
}

// --- Snake flag badge -------------------------------------------------------

const SNAKE_FLAG_CONFIG = {
  VALUE: { label: 'VALUE', color: 'green', className: 'text-emerald-400 bg-emerald-500/15' },
  SLEEPER: { label: 'SLEEPER', color: 'purple', className: 'text-purple-400 bg-purple-500/15' },
  TARGET: { label: 'TARGET', color: 'blue', className: 'text-blue-400 bg-blue-500/15' },
  REACH: { label: 'REACH', color: 'orange', className: 'text-orange-400 bg-orange-500/15' },
}

/** Flag badge config { label, color } — defaults to TARGET for unknown/missing. */
export function getSnakeFlag(player) {
  return SNAKE_FLAG_CONFIG[player?.snake_flag] ?? SNAKE_FLAG_CONFIG.TARGET
}

/** Tailwind classes for a player's snake flag badge (TARGET-styled fallback). */
export function getSnakeFlagClass(player) {
  return (SNAKE_FLAG_CONFIG[player?.snake_flag] ?? SNAKE_FLAG_CONFIG.TARGET).className
}

/** The raw flag label, or null when the player has none (for badge rendering). */
export function getSnakeFlagLabel(player) {
  return player?.snake_flag ?? null
}

// --- Recommendation object (engine output — a DIFFERENT shape) --------------
// Recs may carry adp_rank (your_turn path) or legacy adp_ai (nomination path),
// and use adp_fp (not adp_fantasypros). Centralize that selection here too so
// the recommendation card stays consistent with the board.

/** Rec display ADP — prefer the clean adp_rank, fall back to rounded adp_ai. */
export function getRecAdp(rec) {
  if (rec?.adp_rank != null) return rec.adp_rank
  if (rec?.adp_ai != null) return Math.round(rec.adp_ai)
  return null
}

/** Rec FP ADP (engine field is adp_fp). */
export function getRecFpAdp(rec) {
  return rec?.adp_fp ?? null
}

/** Rec ADP diff. */
export function getRecAdpDiff(rec) {
  return rec?.adp_diff ?? null
}
