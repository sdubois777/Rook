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
  ArrowLeftRight,
  Waves,
  UserCircle,
  Coins,
  X,
} from 'lucide-react'
import { useUIStore } from '../../stores/ui'
import { useMe } from '../../hooks/useMe'
import LeagueSelector from './LeagueSelector'
import Logo from '../brand/Logo'

// Nav grouped into the three areas of the product. Draft Room keeps its amber
// accent (live, full-screen). Grouping is structure, not decoration — a quiet
// section label on the expanded rail, a divider on the collapsed rail.
const navGroups = [
  {
    label: 'Draft',
    items: [
      { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
      { to: '/teams', label: 'Teams', icon: Shield },
      { to: '/draftboard', label: 'Draft Board', icon: ClipboardList },
      { to: '/draft-room', label: 'Draft Room', icon: Swords, accent: true },
    ],
  },
  {
    label: 'In-Season',
    items: [
      { to: '/news', label: 'News', icon: Newspaper },
      { to: '/trade', label: 'Trade', icon: ArrowLeftRight },
      { to: '/waiver', label: 'Waiver Wire', icon: Waves },
      { to: '/matchup', label: 'Matchup', icon: Swords },
    ],
  },
  {
    label: 'Account',
    items: [{ to: '/account', label: 'Account', icon: UserCircle }],
  },
]


// `mobileOpen` / `onClose` drive the off-canvas drawer on small screens; they
// are no-ops at lg, where the sidebar is the existing fixed desktop rail. The
// desktop `collapsed` toggle (Zustand) only applies at lg.
export default function Sidebar({ mobileOpen = false, onClose }) {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggle = useUIStore((s) => s.toggleSidebar)
  const { credits } = useMe()
  // Labels/brand hide only when desktop-collapsed; on mobile the drawer is full
  // width so they always show.
  const labelHidden = collapsed ? 'lg:hidden' : ''
  return (
    <aside
      className={`fixed top-0 left-0 h-full bg-surface-1 border-r border-border flex flex-col transition-transform duration-200 w-64 z-50 ${
        mobileOpen ? 'translate-x-0' : '-translate-x-full'
      } lg:translate-x-0 lg:z-40 lg:transition-all ${
        collapsed ? 'lg:w-16' : 'lg:w-56'
      }`}
    >
      {/* Header */}
      <div className="flex items-center h-14 px-4 border-b border-border">
        {/* Full lockup; the wordmark hides at lg when the rail is collapsed,
            leaving the glyph alone. */}
        <Logo size={24} wordmarkClassName={`text-white ${labelHidden}`} />
        {/* Mobile close (drawer) */}
        <button
          onClick={onClose}
          aria-label="Close navigation"
          className="lg:hidden ml-auto flex items-center justify-center min-h-11 min-w-11 text-slate-400 hover:text-slate-200"
        >
          <X size={20} />
        </button>
        {/* Desktop collapse toggle */}
        <button
          onClick={toggle}
          aria-label="Toggle sidebar"
          className="hidden lg:block ml-auto text-slate-400 hover:text-slate-200 p-1"
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
      </div>

      {/* League selector — persistent across pages */}
      <LeagueSelector />

      {/* Nav — grouped: Draft · In-Season · Account */}
      <nav className="flex-1 py-2">
        {navGroups.map((group, gi) => (
          <div key={group.label} className={gi > 0 ? 'mt-1' : ''}>
            {/* Group separator. Expanded rail: a quiet uppercase label. Collapsed
                rail: a short divider (labels have no room), shown for groups
                after the first so the icon rail still reads as three sections. */}
            {gi > 0 && collapsed && (
              <div className="hidden lg:block mx-3 my-2 border-t border-border" />
            )}
            <div
              className={`px-4 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500 ${labelHidden}`}
            >
              {group.label}
            </div>
            {group.items.map(({ to, label, icon: Icon, accent }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                onClick={onClose}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-4 py-2.5 text-sm transition-colors min-h-11 lg:min-h-0 ${
                    isActive
                      ? 'text-brand-accent bg-brand/10 border-r-2 border-brand-accent'
                      : accent
                        ? 'text-amber-400 hover:text-amber-300 hover:bg-surface-2'
                        : 'text-slate-400 hover:text-slate-200 hover:bg-surface-2'
                  }`
                }
              >
                <Icon size={18} />
                <span className={labelHidden}>{label}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      {/* Footer — credits + user */}
      <div className="p-4 border-t border-border space-y-3">
        {credits !== null && (
          <NavLink
            to="/account"
            onClick={onClose}
            title={`${credits} credits`}
            className="flex items-center gap-2 rounded-md bg-surface-2 px-2.5 py-2 text-sm text-slate-200 hover:bg-surface-3 transition-colors min-h-11 lg:min-h-0"
          >
            <Coins size={16} className="shrink-0 text-amber-400" />
            <span className="font-semibold tabular-nums">{credits}</span>
            <span className={`text-slate-400 ${labelHidden}`}>credits</span>
          </NavLink>
        )}
        <div className="flex items-center gap-2">
          <UserButton afterSignOutUrl="/sign-in" />
          <span className={`text-xs text-slate-400 truncate ${labelHidden}`}>
            My Account
          </span>
        </div>
      </div>
    </aside>
  )
}
