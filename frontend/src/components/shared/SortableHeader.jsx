import { ChevronUp, ChevronDown } from 'lucide-react'

export default function SortableHeader({
  label,
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
      className={`flex items-center gap-0.5 text-[10px] uppercase tracking-wider hover:text-slate-300 transition-colors ${
        isActive ? 'text-blue-400' : 'text-slate-500'
      } ${align === 'right' ? 'justify-end' : ''} ${className}`}
    >
      {label}
      {Icon && <Icon size={10} />}
    </button>
  )
}
