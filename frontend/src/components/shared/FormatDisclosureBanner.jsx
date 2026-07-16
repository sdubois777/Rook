/**
 * Phase 2 disclosure banner. Renders when a non-PPR surface had to fall back to PPR:
 *   - scoringFormatDefaulted: the league's format was null/unsupported/custom → shown as PPR.
 *   - adpFormatDefaulted: values/tier are format-correct, but the market ADP shown is still
 *     PPR because the per-format ADP hasn't been populated by a pipeline run yet.
 * Nothing renders for a clean PPR league or a fully-resolved non-PPR league.
 */
const LABELS = { ppr: 'PPR', half_ppr: 'Half-PPR', standard: 'Standard' }

export default function FormatDisclosureBanner({
  scoringFormat = 'ppr',
  scoringFormatDefaulted = false,
  adpFormatDefaulted = false,
}) {
  if (!scoringFormatDefaulted && !adpFormatDefaulted) return null

  const msg = scoringFormatDefaulted
    ? "Showing PPR values — your league's scoring format wasn't detected (or is custom, approximated as PPR)."
    : `Showing ${LABELS[scoringFormat] || scoringFormat} values — ADP is still PPR until the next data refresh populates per-format ADP.`

  return (
    <div className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
      <span className="font-semibold">Heads up:</span> {msg}
    </div>
  )
}
