export default function ValueComparisonBar({ systemValue, marketValue, maxValue = 100 }) {
  if (systemValue == null || marketValue == null) return null

  const gap = systemValue - marketValue
  const systemPct = Math.min((systemValue / maxValue) * 100, 100)
  const marketPct = Math.min((marketValue / maxValue) * 100, 100)

  let label, labelColor
  if (gap > 3) {
    label = 'Undervalued'
    labelColor = 'text-emerald-400'
  } else if (gap < -3) {
    label = 'Overvalued'
    labelColor = 'text-red-400'
  } else {
    label = 'Aligned'
    labelColor = 'text-slate-400'
  }

  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-slate-400">
        <span>System: ${systemValue.toFixed(0)}</span>
        <span>Market: ${marketValue.toFixed(0)}</span>
      </div>
      <div className="relative h-2 bg-[#1c1f2e] rounded-full overflow-hidden">
        <div
          className="absolute top-0 left-0 h-full bg-blue-500/40 rounded-full"
          style={{ width: `${systemPct}%` }}
        />
        <div
          className="absolute top-0 left-0 h-full bg-slate-400/40 rounded-full"
          style={{ width: `${marketPct}%` }}
        />
      </div>
      <div className={`text-xs font-medium ${labelColor}`}>{label}</div>
    </div>
  )
}
