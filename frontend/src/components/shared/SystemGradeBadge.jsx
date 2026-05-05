const gradeColors = {
  A: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40',
  B: 'bg-teal-500/20 text-teal-400 border-teal-500/40',
  C: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/40',
  D: 'bg-orange-500/20 text-orange-400 border-orange-500/40',
  F: 'bg-red-500/20 text-red-400 border-red-500/40',
}

export default function SystemGradeBadge({ grade, size = 'md' }) {
  if (!grade) return null

  const letter = grade.charAt(0).toUpperCase()
  const colorClass = gradeColors[letter] || gradeColors.F

  const sizeClass = size === 'sm'
    ? 'w-7 h-7 text-xs'
    : size === 'lg'
    ? 'w-11 h-11 text-base'
    : 'w-9 h-9 text-sm'

  return (
    <span
      className={`inline-flex items-center justify-center rounded-full border font-bold ${colorClass} ${sizeClass}`}
      title={`System Grade: ${grade}`}
    >
      {grade}
    </span>
  )
}
