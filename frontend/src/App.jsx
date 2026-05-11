import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useEffect } from 'react'
import { usePreferencesStore } from './stores/preferences'
import Layout from './components/layout/Layout'
import Dashboard from './pages/Dashboard'
import Players from './pages/Players'
import Teams from './pages/Teams'
import TeamDetail from './pages/TeamDetail'
import News from './pages/News'
import DraftBoard from './pages/DraftBoard'
import DraftRoom from './pages/DraftRoom'
import PipelineAdmin from './pages/PipelineAdmin'

// Routes that render full-screen without the sidebar layout
const FULL_SCREEN_ROUTES = ['/draft-room']

function App() {
  const loadWatchlist = usePreferencesStore((s) => s.loadWatchlist)
  const loadStrategy = usePreferencesStore((s) => s.loadStrategy)
  const location = useLocation()

  useEffect(() => {
    loadWatchlist().catch(() => {})
    loadStrategy().catch(() => {})
  }, [loadWatchlist, loadStrategy])

  const isFullScreen = FULL_SCREEN_ROUTES.includes(location.pathname)

  const routes = (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/players" element={<Players />} />
      <Route path="/teams" element={<Teams />} />
      <Route path="/teams/:abbr" element={<TeamDetail />} />
      <Route path="/news" element={<News />} />
      <Route path="/draftboard" element={<DraftBoard />} />
      <Route path="/draft-room" element={<DraftRoom />} />
      <Route path="/admin" element={<PipelineAdmin />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )

  if (isFullScreen) {
    return routes
  }

  return <Layout>{routes}</Layout>
}

export default App
