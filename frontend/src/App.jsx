import { Routes, Route, Navigate } from 'react-router-dom'
import { useEffect } from 'react'
import { usePreferencesStore } from './stores/preferences'
import Layout from './components/layout/Layout'
import Dashboard from './pages/Dashboard'
import Players from './pages/Players'
import Teams from './pages/Teams'
import TeamDetail from './pages/TeamDetail'
import News from './pages/News'
import DraftBoard from './pages/DraftBoard'
import PipelineAdmin from './pages/PipelineAdmin'

function App() {
  const loadWatchlist = usePreferencesStore((s) => s.loadWatchlist)
  const loadStrategy = usePreferencesStore((s) => s.loadStrategy)

  useEffect(() => {
    loadWatchlist().catch(() => {})
    loadStrategy().catch(() => {})
  }, [loadWatchlist, loadStrategy])

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/players" element={<Players />} />
        <Route path="/teams" element={<Teams />} />
        <Route path="/teams/:abbr" element={<TeamDetail />} />
        <Route path="/news" element={<News />} />
        <Route path="/draftboard" element={<DraftBoard />} />
        <Route path="/admin" element={<PipelineAdmin />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}

export default App
