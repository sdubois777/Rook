import Sidebar from './Sidebar'
import AssistantButton from '../AssistantButton'
import AssistantPanel from '../AssistantPanel'
import { useUIStore } from '../../stores/ui'

export default function Layout({ children }) {
  const collapsed = useUIStore((s) => s.sidebarCollapsed)

  return (
    <div className="flex h-screen bg-[#0f1117] text-slate-200">
      <Sidebar />
      <main
        className={`flex-1 overflow-y-auto transition-all duration-200 ${
          collapsed ? 'ml-16' : 'ml-56'
        }`}
      >
        <div className="p-6">{children}</div>
      </main>
      <AssistantButton />
      <AssistantPanel />
    </div>
  )
}
