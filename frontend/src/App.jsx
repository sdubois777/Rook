import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useEffect } from 'react'
import { SignedIn, SignedOut, RedirectToSignIn } from '@clerk/clerk-react'
import { usePreferencesStore } from './stores/preferences'
import Layout from './components/layout/Layout'
import Landing from './pages/Landing'
import Pricing from './pages/Pricing'
import Dashboard from './pages/Dashboard'
import Players from './pages/Players'
import Teams from './pages/Teams'
import TeamDetail from './pages/TeamDetail'
import News from './pages/News'
import DraftBoard from './pages/DraftBoard'
import DraftRoom from './pages/DraftRoom'
import PipelineAdmin from './pages/PipelineAdmin'
import SignInPage from './pages/SignIn'
import SignUpPage from './pages/SignUp'
import AccountPage from './pages/Account'
import LeagueSetup from './pages/LeagueSetup'

// Routes that render full-screen without the sidebar layout
const FULL_SCREEN_ROUTES = ['/draft-room']

// Public routes — no sidebar, no auth required
const PUBLIC_ROUTES = ['/', '/pricing', '/sign-in', '/sign-up']

function ProtectedRoute({ children }) {
  return (
    <>
      <SignedIn>{children}</SignedIn>
      <SignedOut><RedirectToSignIn /></SignedOut>
    </>
  )
}

function App() {
  const loadWatchlist = usePreferencesStore((s) => s.loadWatchlist)
  const loadStrategy = usePreferencesStore((s) => s.loadStrategy)
  const location = useLocation()

  useEffect(() => {
    loadWatchlist().catch(() => {})
    loadStrategy().catch(() => {})
  }, [loadWatchlist, loadStrategy])

  const isPublic = PUBLIC_ROUTES.some(
    (r) => location.pathname === r || location.pathname.startsWith(r + '/')
  )

  // Public routes — different layout, no sidebar
  if (isPublic) {
    return (
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/pricing" element={<Pricing />} />
        <Route path="/sign-in/*" element={<SignInPage />} />
        <Route path="/sign-up/*" element={<SignUpPage />} />
      </Routes>
    )
  }

  const isFullScreen = FULL_SCREEN_ROUTES.includes(location.pathname)

  const routes = (
    <Routes>
      <Route path="/dashboard" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
      <Route path="/players" element={<ProtectedRoute><Players /></ProtectedRoute>} />
      <Route path="/teams" element={<ProtectedRoute><Teams /></ProtectedRoute>} />
      <Route path="/teams/:abbr" element={<ProtectedRoute><TeamDetail /></ProtectedRoute>} />
      <Route path="/news" element={<ProtectedRoute><News /></ProtectedRoute>} />
      <Route path="/draftboard" element={<ProtectedRoute><DraftBoard /></ProtectedRoute>} />
      <Route path="/draft-room" element={<ProtectedRoute><DraftRoom /></ProtectedRoute>} />
      <Route path="/admin" element={<ProtectedRoute><PipelineAdmin /></ProtectedRoute>} />
      <Route path="/account" element={<ProtectedRoute><AccountPage /></ProtectedRoute>} />
      <Route path="/league-setup" element={<ProtectedRoute><LeagueSetup /></ProtectedRoute>} />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )

  if (isFullScreen) {
    return routes
  }

  return <Layout>{routes}</Layout>
}

export default App
