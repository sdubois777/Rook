import { ChevronLeft, ChevronRight } from 'lucide-react'

export default function Pagination({ page, pages, onPageChange }) {
  if (pages <= 1) return null

  return (
    <div className="flex items-center justify-center gap-3 py-4">
      <button
        onClick={() => onPageChange(page - 1)}
        disabled={page <= 1}
        className="flex items-center gap-1 px-3 py-1.5 text-xs text-slate-400 bg-surface-2 rounded border border-border hover:bg-surface-3 disabled:opacity-30 disabled:cursor-not-allowed"
      >
        <ChevronLeft size={14} />
        Previous
      </button>
      <span className="text-xs text-slate-500">
        Page {page} of {pages}
      </span>
      <button
        onClick={() => onPageChange(page + 1)}
        disabled={page >= pages}
        className="flex items-center gap-1 px-3 py-1.5 text-xs text-slate-400 bg-surface-2 rounded border border-border hover:bg-surface-3 disabled:opacity-30 disabled:cursor-not-allowed"
      >
        Next
        <ChevronRight size={14} />
      </button>
    </div>
  )
}
