// The AUTHORITATIVE position palette, app-wide. Trade/Waiver/Matchup previously
// forked a divergent palette (QB rose, WR sky, K violet) — that fork is removed;
// everything renders these colors now. Two densities, SAME palette (recon area 6):
//   dense   — bordered text-xs (draft board/room, dashboard, detail). DEFAULT, so
//             existing <PositionBadge position=.. /> callers are pixel-identical.
//   compact — borderless text-[10px] (the Trade/Waiver row look).
// Full static class strings (Tailwind JIT can't see interpolated names).
const DENSE = {
  QB: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  RB: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  WR: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  TE: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  K: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  DEF: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
}
const DENSE_FALLBACK = 'bg-slate-500/20 text-slate-400 border-slate-500/30'

const COMPACT = {
  QB: 'bg-purple-500/20 text-purple-400',
  RB: 'bg-emerald-500/20 text-emerald-400',
  WR: 'bg-blue-500/20 text-blue-400',
  TE: 'bg-orange-500/20 text-orange-400',
  K: 'bg-amber-500/20 text-amber-400',
  DEF: 'bg-cyan-500/20 text-cyan-400',
}
const COMPACT_FALLBACK = 'bg-slate-500/15 text-slate-300'

export default function PositionBadge({ position, variant = 'dense' }) {
  if (!position) return null
  if (variant === 'compact') {
    const style = COMPACT[position] || COMPACT_FALLBACK
    return <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold shrink-0 ${style}`}>{position}</span>
  }
  const style = DENSE[position] || DENSE_FALLBACK
  return (
    <span className={`inline-flex items-center px-2 py-0.5 text-xs font-semibold rounded border shrink-0 ${style}`}>
      {position}
    </span>
  )
}
