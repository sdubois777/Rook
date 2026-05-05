const flagStyles = {
  DISPLACED: { bg: 'bg-red-500/15', text: 'text-red-400', label: 'Displaced' },
  CONTINGENT: { bg: 'bg-amber-500/15', text: 'text-amber-400', label: 'Contingent' },
  ELEVATED: { bg: 'bg-emerald-500/15', text: 'text-emerald-400', label: 'Elevated' },
  DIMINISHED: { bg: 'bg-orange-500/15', text: 'text-orange-400', label: 'Diminished' },
  COMMITTEE: { bg: 'bg-yellow-500/15', text: 'text-yellow-400', label: 'Committee' },
  SCHEME_CHANGE: { bg: 'bg-purple-500/15', text: 'text-purple-400', label: 'Scheme Change' },
  QB_DOWNGRADE: { bg: 'bg-red-500/15', text: 'text-red-400', label: 'QB Downgrade' },
  QB_UPGRADE: { bg: 'bg-emerald-500/15', text: 'text-emerald-400', label: 'QB Upgrade' },
  OL_DOWNGRADE: { bg: 'bg-orange-500/15', text: 'text-orange-400', label: 'OL Downgrade' },
  OL_UPGRADE: { bg: 'bg-teal-500/15', text: 'text-teal-400', label: 'OL Upgrade' },
  ROOKIE_COMPETITION: { bg: 'bg-cyan-500/15', text: 'text-cyan-400', label: 'Rookie Comp' },
  COMPOUND_RISK: { bg: 'bg-red-500/15', text: 'text-red-400', label: 'Compound Risk' },
}

export default function FlagBadge({ flagType, compact = false }) {
  const style = flagStyles[flagType] || {
    bg: 'bg-slate-500/15',
    text: 'text-slate-400',
    label: flagType,
  }

  return (
    <span
      className={`inline-flex items-center rounded-full font-medium ${style.bg} ${style.text} ${
        compact ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs'
      }`}
      title={flagType}
    >
      {style.label}
    </span>
  )
}
