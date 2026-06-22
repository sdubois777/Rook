/**
 * Rook brand lockup — the navy R-glyph (identical to favicon.svg) plus the
 * "Rook" wordmark. The wordmark is live text in the system font, so it stays
 * crisp at any size and recolors via `wordmarkClassName`.
 *
 *   <Logo />                       full lockup, white wordmark (dark surfaces)
 *   <Logo withWordmark={false} />  glyph only (collapsed sidebar rail)
 *   <Logo wordmarkClassName="lg:hidden" />  hide just the wordmark responsively
 *
 * The glyph fill is the literal brand navy (#2a3d8f) — it is the canonical mark
 * and is navy in every context, exactly like the favicon.
 */
export default function Logo({
  size = 28,
  withWordmark = true,
  className = '',
  wordmarkClassName = 'text-white',
}) {
  return (
    <span
      className={`inline-flex items-center ${className}`}
      style={{ gap: Math.round(size * 0.3) }}
      aria-label="Rook"
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 100 100"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
        className="shrink-0"
      >
        <rect width="100" height="100" rx="23" fill="#2a3d8f" />
        <path
          d="M37 26 L37 74 M37 26 L64 26 Q79 26 79 40 Q79 54 64 54 L37 54 M59 54 L80 74"
          fill="none"
          stroke="#ffffff"
          strokeWidth="11"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      {withWordmark && (
        <span
          className={`font-bold tracking-tight leading-none ${wordmarkClassName}`}
          style={{ fontSize: Math.round(size * 0.82) }}
        >
          Rook
        </span>
      )}
    </span>
  )
}
