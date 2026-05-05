import { useState, useEffect, useRef } from 'react'
import { Search, X } from 'lucide-react'

export default function SearchInput({ placeholder = 'Search...', onSearch, delay = 300 }) {
  const [value, setValue] = useState('')
  const timerRef = useRef(null)

  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => {
      onSearch(value)
    }, delay)
    return () => clearTimeout(timerRef.current)
  }, [value, delay, onSearch])

  return (
    <div className="relative">
      <Search
        size={14}
        className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500"
      />
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={placeholder}
        className="w-full pl-8 pr-8 py-1.5 text-sm bg-[#1c1f2e] text-slate-300 border border-[#2d3148] rounded focus:outline-none focus:border-blue-500/50 placeholder-slate-600"
      />
      {value && (
        <button
          onClick={() => setValue('')}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
        >
          <X size={14} />
        </button>
      )}
    </div>
  )
}
