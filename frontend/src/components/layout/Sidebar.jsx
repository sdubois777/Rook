import { NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  LayoutDashboard,
  Users,
  Shield,
  Newspaper,
  ClipboardList,
  Settings,
  ChevronLeft,
  ChevronRight,
  Swords,
} from 'lucide-react'
import { useUIStore } from '../../stores/ui'
import { fetchPipelineStatus } from '../../api/admin'

const navItems = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/players', label: 'Players', icon: Users },
  { to: '/teams', label: 'Teams', icon: Shield },
  { to: '/news', label: 'News', icon: Newspaper },
  { to: '/draftboard', label: 'Draft Board', icon: ClipboardList },
  { to: '/admin', label: 'Pipeline', icon: Settings },
]

function usePipelineFreshness() {
  const { data } = useQuery({
    queryKey: ['pipeline-status'],
    queryFn: fetchPipelineStatus,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  if (!data?.agents) return 'unknown'
  const staleCount = data.agents.filter((a) => a.stale).length
  if (staleCount === 0) return 'fresh'
  if (staleCount <= 2) return 'warning'
  return 'stale'
}

const freshnessColors = {
  fresh: 'bg-emerald-400',
  warning: 'bg-yellow-400',
  stale: 'bg-red-400',
  unknown: 'bg-slate-500',
}

const freshnessLabels = {
  fresh: 'All agents fresh',
  warning: 'Some agents stale',
  stale: 'Most agents stale',
  unknown: 'Checking...',
}

export default function Sidebar() {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggle = useUIStore((s) => s.toggleSidebar)
  const freshness = usePipelineFreshness()

  return (
    <aside
      className={`fixed top-0 left-0 h-full bg-[#161822] border-r border-[#2d3148] flex flex-col transition-all duration-200 z-40 ${
        collapsed ? 'w-16' : 'w-56'
      }`}
    >
      {/* Header */}
      <div className="flex items-center h-14 px-4 border-b border-[#2d3148]">
        {!collapsed && (
          <span className="text-blue-400 font-semibold text-sm tracking-wide">
            Fantasy Manager
          </span>
        )}
        <button
          onClick={toggle}
          className="ml-auto text-slate-400 hover:text-slate-200 p-1"
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
      </div>

      {/* Nav */}
      <nav className="flex-1 py-2">
        {navItems.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${
                isActive
                  ? 'text-blue-400 bg-blue-500/10 border-r-2 border-blue-400'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-[#1c1f2e]'
              }`
            }
          >
            <Icon size={18} />
            {!collapsed && <span>{label}</span>}
          </NavLink>
        ))}

        {/* Draft Room — full-screen live draft */}
        <NavLink
          to="/draft-room"
          className={({ isActive }) =>
            `flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${
              isActive
                ? 'text-blue-400 bg-blue-500/10 border-r-2 border-blue-400'
                : 'text-amber-400 hover:text-amber-300 hover:bg-[#1c1f2e]'
            }`
          }
        >
          <Swords size={18} />
          {!collapsed && <span>Draft Room</span>}
        </NavLink>
      </nav>

      {/* Footer — pipeline freshness */}
      <div className="p-4 border-t border-[#2d3148] flex items-center gap-2">
        <span
          className={`w-2 h-2 rounded-full shrink-0 ${freshnessColors[freshness]}`}
          title={freshnessLabels[freshness]}
        />
        {!collapsed && (
          <span className="text-xs text-slate-500">{freshnessLabels[freshness]}</span>
        )}
      </div>
    </aside>
  )
}
