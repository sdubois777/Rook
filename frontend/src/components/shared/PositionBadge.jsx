const positionStyles = {
  QB: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  RB: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  WR: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  TE: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
}

export default function PositionBadge({ position }) {
  if (!position) return null

  const style = positionStyles[position] || 'bg-slate-500/20 text-slate-400 border-slate-500/30'

  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 text-xs font-semibold rounded border shrink-0 ${style}`}
    >
      {position}
    </span>
  )
}
