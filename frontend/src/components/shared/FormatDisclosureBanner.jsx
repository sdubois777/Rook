/**
 * Disclosure banner for a non-PPR league. Renders only when something is genuinely wrong:
 *
 *   - scoringFormatDefaulted: the league's scoring format was null, unsupported or custom,
 *     so the whole board is showing PPR values.
 *   - adpFormatDefaulted / marketFormatDefaulted: this scoring format has NO per-format
 *     market data at all, so every row's ADP or market price is the PPR figure.
 *
 * THE LAST TWO MEAN "NEVER POPULATED", NOT "INCOMPLETE". They used to be set as soon as a
 * single player lacked a per-format figure, which made this banner permanently visible for
 * Half-PPR and Standard: FantasyPros publishes about 340 ADP entries for those formats
 * while the board shows roughly 386 players, so dozens of rows can never have one. The
 * always-on warning was then read as "the weekly data refresh is broken" when it was not.
 * Partial coverage is normal and is not disclosed here; the backend returns
 * adp_format_rows / market_format_rows for anyone who wants the exact count.
 */
const LABELS = { ppr: 'PPR', half_ppr: 'Half-PPR', standard: 'Standard' }

export default function FormatDisclosureBanner({
  scoringFormat = 'ppr',
  scoringFormatDefaulted = false,
  adpFormatDefaulted = false,
  marketFormatDefaulted = false,
}) {
  if (!scoringFormatDefaulted && !adpFormatDefaulted && !marketFormatDefaulted) return null

  const label = LABELS[scoringFormat] || scoringFormat
  // Both market columns can be missing at once; name whichever ones actually are.
  const missing = [
    adpFormatDefaulted ? 'ADP' : null,
    marketFormatDefaulted ? 'market prices' : null,
  ].filter(Boolean).join(' and ')

  const msg = scoringFormatDefaulted
    ? "Showing PPR values — your league's scoring format wasn't detected (or is custom, approximated as PPR)."
    : `Showing ${label} values, but no ${label} ${missing} could be found — those columns are PPR figures. This usually means the data refresh hasn't run for this format yet.`

  return (
    <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
      <span className="font-semibold">Heads up:</span> {msg}
    </div>
  )
}
