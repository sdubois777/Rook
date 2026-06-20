import { useState } from 'react'
import { Menu } from 'lucide-react'
import Sidebar from './Sidebar'
import AssistantButton from '../AssistantButton'
import AssistantPanel from '../AssistantPanel'
import { useUIStore } from '../../stores/ui'

export default function Layout({ children }) {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)
  // Mobile off-canvas nav — local presentational state (not the Zustand store).
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  return (
    // Column on mobile (top bar above content); the existing row layout returns
    // at lg. The Sidebar is fixed/out-of-flow, so `main` is the only flow child.
    <div className="flex flex-col lg:flex-row h-screen bg-[#0f1117] text-slate-200">
      {/* Mobile top bar — hidden at lg so desktop is unchanged. */}
      <header className="lg:hidden flex items-center gap-2 h-14 px-3 border-b border-[#2d3148] bg-[#161822] shrink-0">
        <button
          onClick={() => setMobileNavOpen(true)}
          aria-label="Open navigation"
          className="flex items-center justify-center min-h-11 min-w-11 -ml-1 text-slate-300 hover:text-slate-100"
        >
          <Menu size={22} />
        </button>
        <span className="text-blue-400 font-semibold text-sm tracking-wide">Rook</span>
      </header>

      {/* Backdrop behind the drawer (mobile only). */}
      {mobileNavOpen && (
        <div
          className="lg:hidden fixed inset-0 bg-black/50 z-40"
          onClick={() => setMobileNavOpen(false)}
          aria-hidden="true"
        />
      )}

      <Sidebar mobileOpen={mobileNavOpen} onClose={() => setMobileNavOpen(false)} />

      <main
        className={`flex-1 overflow-y-auto transition-all duration-200 ml-0 ${
          collapsed ? 'lg:ml-16' : 'lg:ml-56'
        }`}
      >
        <div className="p-4 lg:p-6">{children}</div>
      </main>
      <AssistantButton />
      <AssistantPanel />
    </div>
  )
}
