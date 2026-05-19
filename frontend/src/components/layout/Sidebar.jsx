import { NavLink } from 'react-router-dom'
import { UserButton } from '@clerk/clerk-react'
import {
  LayoutDashboard,
  Shield,
  Newspaper,
  ClipboardList,
  ChevronLeft,
  ChevronRight,
  Swords,
  UserCircle,
} from 'lucide-react'
import { useUIStore } from '../../stores/ui'

const navItems = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/teams', label: 'Teams', icon: Shield },
  { to: '/news', label: 'News', icon: Newspaper },
  { to: '/draftboard', label: 'Draft Board', icon: ClipboardList },
  { to: '/account', label: 'Account', icon: UserCircle },
]


export default function Sidebar() {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggle = useUIStore((s) => s.toggleSidebar)
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

      {/* Footer — user */}
      <div className="p-4 border-t border-[#2d3148]">
        <div className="flex items-center gap-2">
          <UserButton afterSignOutUrl="/sign-in" />
          {!collapsed && (
            <span className="text-xs text-slate-400 truncate">My Account</span>
          )}
        </div>
      </div>
    </aside>
  )
}
