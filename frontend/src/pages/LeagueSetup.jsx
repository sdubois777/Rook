import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '@clerk/clerk-react'
import { apiClient } from '../api/client'
import { getBookmarkletCode } from '../utils/espnBookmarklet'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const PLATFORMS = [
  { id: 'yahoo', name: 'Yahoo', color: 'bg-purple-600 hover:bg-purple-500', icon: '🟣' },
  { id: 'espn', name: 'ESPN', color: 'bg-orange-600 hover:bg-orange-500', icon: '🏈' },
  { id: 'sleeper', name: 'Sleeper', color: 'bg-sky-600 hover:bg-sky-500', icon: '💤' },
]

const STEPS = ['Platform', 'Connect', 'Select League', 'Confirm', 'Import']

function StepIndicator({ current }) {
  return (
    <div className="flex items-center gap-2 mb-8">
      {STEPS.map((label, i) => (
        <div key={label} className="flex items-center gap-2">
          <div
            className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${
              i < current
                ? 'bg-green-600 text-white'
                : i === current
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-800 text-gray-500'
            }`}
          >
            {i < current ? '✓' : i + 1}
          </div>
          <span
            className={`text-sm hidden sm:inline ${
              i === current ? 'text-white' : 'text-gray-500'
            }`}
          >
            {label}
          </span>
          {i < STEPS.length - 1 && (
            <div className={`w-8 h-px ${i < current ? 'bg-green-600' : 'bg-gray-700'}`} />
          )}
        </div>
      ))}
    </div>
  )
}

// Step 1 — Choose Platform
function PlatformStep({ onSelect }) {
  return (
    <div>
      <h2 className="text-2xl font-bold mb-2">Choose Your Platform</h2>
      <p className="text-gray-400 mb-8">Which platform is your fantasy league on?</p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {PLATFORMS.map((p) => (
          <button
            key={p.id}
            onClick={() => onSelect(p.id)}
            className={`${p.color} text-white rounded-xl p-6 text-center transition-colors`}
          >
            <div className="text-3xl mb-2">{p.icon}</div>
            <div className="text-lg font-semibold">{p.name}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

// Step 2 — Connect (platform-specific)
function ConnectStep({ platform, onConnected, onBack }) {
  if (platform === 'yahoo') return <YahooConnect onBack={onBack} />
  if (platform === 'espn') return <EspnConnect onConnected={onConnected} onBack={onBack} />
  if (platform === 'sleeper') return <SleeperConnect onConnected={onConnected} onBack={onBack} />
  return null
}

function YahooConnect({ onBack }) {
  const { getToken } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleConnect = async () => {
    setError('')
    setLoading(true)
    try {
      // Step 1: authenticated fetch to get the Yahoo OAuth URL
      const token = await getToken()
      const response = await fetch(`${API_URL}/auth/yahoo/connect-url`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!response.ok) throw new Error('Failed to get Yahoo OAuth URL')
      const { url } = await response.json()

      // Step 2: browser navigates to Yahoo (no CORS issue)
      window.location.href = url
    } catch {
      setError('Failed to start Yahoo connection. Please try again.')
      setLoading(false)
    }
  }

  return (
    <div>
      <h2 className="text-2xl font-bold mb-2">Connect Yahoo Fantasy</h2>
      <p className="text-gray-400 mb-8">
        You'll be redirected to Yahoo to authorize access to your leagues.
      </p>
      {error && <p className="text-red-400 text-sm mb-4">{error}</p>}
      <button
        onClick={handleConnect}
        disabled={loading}
        className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white font-medium px-6 py-3 rounded-lg transition-colors"
      >
        {loading ? 'Connecting...' : 'Connect with Yahoo'}
      </button>
      <BackButton onClick={onBack} />
    </div>
  )
}

function EspnConnect({ onConnected, onBack }) {
  const [showManual, setShowManual] = useState(false)
  const [espnS2, setEspnS2] = useState('')
  const [swid, setSwid] = useState('')
  const [leagueId, setLeagueId] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleManualSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const resp = await apiClient.post('/leagues/connect/espn', {
        league_id: leagueId,
        espn_s2: espnS2,
        swid,
      })
      onConnected(resp.data)
    } catch (err) {
      setError(err.response?.data?.message || 'Failed to connect ESPN league')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <h2 className="text-2xl font-bold mb-2">Connect Your ESPN League</h2>
      <p className="text-gray-400 mb-6">
        Make sure you're logged in to ESPN Fantasy, then drag the button below to your bookmarks bar.
        Visit your ESPN league page and click the bookmark.
      </p>

      <a
        href={getBookmarkletCode(API_URL)}
        className="bg-orange-600 hover:bg-orange-500 text-white font-medium px-6 py-3 rounded-lg inline-flex items-center gap-2 transition-colors cursor-grab"
        onClick={(e) => e.preventDefault()}
        draggable
      >
        DraftMind ESPN Connect
      </a>

      <p className="text-sm text-gray-500 mt-3 mb-6">
        Drag this to your bookmarks bar, then click it on your ESPN Fantasy league page.
      </p>

      <button
        onClick={() => setShowManual(!showManual)}
        className="text-sm text-gray-400 hover:text-gray-300"
      >
        {showManual ? 'Hide manual entry' : 'Having trouble? Enter cookies manually'}
      </button>

      {showManual && (
        <form onSubmit={handleManualSubmit} className="mt-4 space-y-4 max-w-md">
          <div>
            <label className="block text-sm text-gray-400 mb-1">League ID</label>
            <input
              type="text"
              value={leagueId}
              onChange={(e) => setLeagueId(e.target.value)}
              placeholder="12345678"
              required
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-400 mb-1">espn_s2 cookie</label>
            <input
              type="text"
              value={espnS2}
              onChange={(e) => setEspnS2(e.target.value)}
              placeholder="Paste espn_s2 value"
              required
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-400 mb-1">SWID cookie</label>
            <input
              type="text"
              value={swid}
              onChange={(e) => setSwid(e.target.value)}
              placeholder="{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
              required
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white"
            />
          </div>
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="bg-orange-600 hover:bg-orange-500 disabled:opacity-50 text-white font-medium px-6 py-2 rounded-lg transition-colors"
          >
            {loading ? 'Connecting...' : 'Connect ESPN League'}
          </button>
        </form>
      )}

      <BackButton onClick={onBack} />
    </div>
  )
}

function SleeperConnect({ onConnected, onBack }) {
  const [username, setUsername] = useState('')
  const [leagueId, setLeagueId] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const resp = await apiClient.post('/leagues/connect/sleeper', {
        username,
        league_id: leagueId,
      })
      onConnected(resp.data)
    } catch (err) {
      setError(err.response?.data?.message || 'Failed to connect Sleeper league')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <h2 className="text-2xl font-bold mb-2">Connect Sleeper League</h2>
      <p className="text-gray-400 mb-6">
        Enter your Sleeper username and league ID. No login required — Sleeper's API is public.
      </p>

      <form onSubmit={handleSubmit} className="space-y-4 max-w-md">
        <div>
          <label className="block text-sm text-gray-400 mb-1">Sleeper Username</label>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="your_sleeper_name"
            required
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white"
          />
        </div>
        <div>
          <label className="block text-sm text-gray-400 mb-1">League ID</label>
          <input
            type="text"
            value={leagueId}
            onChange={(e) => setLeagueId(e.target.value)}
            placeholder="123456789012345678"
            required
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white"
          />
        </div>
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <button
          type="submit"
          disabled={loading}
          className="bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white font-medium px-6 py-2 rounded-lg transition-colors"
        >
          {loading ? 'Connecting...' : 'Connect Sleeper League'}
        </button>
      </form>

      <BackButton onClick={onBack} />
    </div>
  )
}

// Step 4 — Confirm settings (shown when direct connect returns data)
function ConfirmStep({ result }) {
  const navigate = useNavigate()

  return (
    <div>
      <h2 className="text-2xl font-bold mb-2">League Connected!</h2>
      <p className="text-gray-400 mb-6">Your league has been imported successfully.</p>

      <div className="bg-gray-800 rounded-xl p-6 space-y-3 max-w-md mb-8">
        <SummaryRow label="Platform" value={result.platform} />
        <SummaryRow label="Draft picks imported" value={result.picks_imported} />
        <SummaryRow label="Seasons imported" value={result.seasons_imported} />
        <SummaryRow label="Managers found" value={result.managers_found} />
        <SummaryRow label="Free agents cached" value={result.free_agents_cached} />
      </div>

      <button
        onClick={() => navigate('/dashboard')}
        className="bg-blue-600 hover:bg-blue-500 text-white font-medium px-6 py-3 rounded-lg transition-colors"
      >
        Go to Dashboard
      </button>
    </div>
  )
}

function SummaryRow({ label, value }) {
  return (
    <div className="flex justify-between text-sm">
      <span className="text-gray-400">{label}</span>
      <span className="text-white font-medium">{value ?? '—'}</span>
    </div>
  )
}

function BackButton({ onClick }) {
  return (
    <button
      onClick={onClick}
      className="block mt-6 text-sm text-gray-500 hover:text-gray-300 transition-colors"
    >
      &larr; Back
    </button>
  )
}

export default function LeagueSetup() {
  const [searchParams] = useSearchParams()
  const [step, setStep] = useState(0)
  const [platform, setPlatform] = useState(null)
  const [result, setResult] = useState(null)

  // Handle ESPN bookmarklet redirect (?platform=espn)
  useEffect(() => {
    const p = searchParams.get('platform')
    if (p) {
      setPlatform(p)
      setStep(1)
    }
  }, [searchParams])

  const handlePlatformSelect = (id) => {
    setPlatform(id)
    setStep(1)
  }

  const handleConnected = (data) => {
    setResult(data)
    setStep(4)
  }

  const handleBack = () => {
    if (step === 1) {
      setPlatform(null)
      setStep(0)
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <div className="max-w-2xl mx-auto px-6 py-12">
        <h1 className="text-3xl font-bold mb-2">Add a League</h1>
        <p className="text-gray-400 mb-8">
          Connect your fantasy league to get AI-powered draft analysis.
        </p>

        <StepIndicator current={step} />

        {step === 0 && <PlatformStep onSelect={handlePlatformSelect} />}
        {step === 1 && (
          <ConnectStep
            platform={platform}
            onConnected={handleConnected}
            onBack={handleBack}
          />
        )}
        {step === 4 && result && <ConfirmStep result={result} />}
      </div>
    </div>
  )
}
