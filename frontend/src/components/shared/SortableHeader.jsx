import { ChevronUp, ChevronDown } from 'lucide-react'

export default function SortableHeader({
  label,
  // Narrow phone-tier label, swapped back to `label` at sm. Defaults to `label`, so every
  // existing call site renders byte-identically. Exists because the HEADER text, not the
  // value, is what constrains a mobile column: "AI ADP" plus its chevron is ~54px at
  // text-[10px] and would wrap to two lines inside a 40px cell, breaking header height.
  shortLabel = null,
  sortKey,
  currentSort,
  currentOrder,
  onSort,
  className = '',
  align = 'left',
  defaultOrder = 'desc',
}) {
  const isActive = currentSort === sortKey

  const handleClick = () => {
    if (!isActive) {
      onSort(sortKey, defaultOrder)
    } else {
      onSort(sortKey, currentOrder === 'asc' ? 'desc' : 'asc')
    }
  }

  const Icon = isActive
    ? currentOrder === 'asc' ? ChevronUp : ChevronDown
    : null

  return (
    <button
      onClick={handleClick}
      className={`flex items-center gap-0.5 text-[10px] uppercase tracking-wider whitespace-nowrap hover:text-slate-300 transition-colors ${
        isActive ? 'text-blue-400' : 'text-slate-500'
      } ${align === 'right' ? 'justify-end' : ''} ${className}`}
    >
      {shortLabel ? (
        <>
          <span className="sm:hidden">{shortLabel}</span>
          <span className="hidden sm:inline">{label}</span>
        </>
      ) : (
        label
      )}
      {Icon && <Icon size={10} />}
    </button>
  )
}
