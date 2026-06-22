export default function FilterBar({ children }) {
  return (
    <div className="flex flex-wrap items-center gap-3 p-3 bg-surface-1 rounded-lg border border-border mb-4">
      {children}
    </div>
  )
}

export function FilterSelect({ label, value, onChange, options }) {
  return (
    <div className="flex items-center gap-2">
      <label className="text-xs text-slate-500">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-surface-2 text-sm text-slate-300 border border-border rounded px-2 py-1 focus:outline-none focus:border-brand-accent/60"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  )
}
