import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { apiClient, API_BASE } from '../api/client'
import { fetchYahooConnectUrl } from '../api/league'
import { DRAFT_LABELS, SCORING_LABELS } from '../lib/constants'
import { getBookmarkletCode } from '../utils/espnBookmarklet'

// Single source of truth for the API base (includes /api) — see api/client.js.
const API_URL = API_BASE

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

// Step 0 — Choose Platform
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

// Step 1 — Connect (platform-specific)
function ConnectStep({ platform, onConnected, onYahooLeagues, onBack }) {
  if (platform === 'yahoo') return <YahooConnect onYahooLeagues={onYahooLeagues} onBack={onBack} />
  if (platform === 'espn') return <EspnConnect onConnected={onConnected} onBack={onBack} />
  if (platform === 'sleeper') return <SleeperConnect onConnected={onConnected} onBack={onBack} />
  return null
}

function YahooConnect({ onYahooLeagues, onBack }) {
  const [checking, setChecking] = useState(true)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // On mount, check if Yahoo is already connected by fetching leagues
  useEffect(() => {
    const checkConnection = async () => {
      try {
        const resp = await apiClient.get('/auth/yahoo/leagues')
        onYahooLeagues(resp.data.leagues)
      } catch {
        // Not connected — show the connect button
        setChecking(false)
      }
    }
    checkConnection()
  }, [])

  const handleConnect = async () => {
    setError('')
    setLoading(true)
    try {
      // Step 1: authenticated request for the Yahoo OAuth URL
      const url = await fetchYahooConnectUrl()

      // Step 2: browser navigates to Yahoo (no CORS issue)
      window.location.href = url
    } catch {
      setError('Failed to start Yahoo connection. Please try again.')
      setLoading(false)
    }
  }

  if (checking) {
    return (
      <div>
        <h2 className="text-2xl font-bold mb-2">Checking Yahoo Connection...</h2>
        <p className="text-gray-400">Looking for your Yahoo Fantasy account...</p>
      </div>
    )
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

// Step 2 — Yahoo League Selection (fetches full settings on select)
function YahooLeagueSelect({ leagues, onSelect, onBack }) {
  const [selected, setSelected] = useState(null)
  const [loadingKey, setLoadingKey] = useState(null)
  const [error, setError] = useState('')

  if (!leagues || leagues.length === 0) {
    return (
      <div>
        <h2 className="text-2xl font-bold mb-2">No Leagues Found</h2>
        <p className="text-gray-400 mb-6">
          No Yahoo Fantasy Football leagues were found for your account.
        </p>
        <BackButton onClick={onBack} />
      </div>
    )
  }

  const handleSelect = async (league) => {
    setSelected(league)
    setError('')
    setLoadingKey(league.league_key)
    try {
      const resp = await apiClient.get(
        `/auth/yahoo/league-settings?league_key=${encodeURIComponent(league.league_key)}`
      )
      const settings = resp.data.settings
      // Merge fetched settings into league object
      setSelected({
        ...league,
        scoring_type: settings.scoring_type,
        draft_type: settings.draft_type,
        num_teams: settings.num_teams,
        name: settings.name || league.name,
        auction_budget: settings.auction_budget,
        playoff_start_week: settings.playoff_start_week,
        uses_faab: settings.uses_faab,
        _settings_loaded: true,
      })
    } catch {
      setError('Could not fetch league settings. You can still continue with basic info.')
      setSelected({ ...league, _settings_loaded: false })
    } finally {
      setLoadingKey(null)
    }
  }

  return (
    <div>
      <h2 className="text-2xl font-bold mb-2">Select Your League</h2>
      <p className="text-gray-400 mb-6">Choose the league you want to import.</p>

      <div className="space-y-3 max-w-md mb-8">
        {leagues.map((league) => (
          <button
            key={league.league_key}
            onClick={() => handleSelect(league)}
            disabled={loadingKey !== null}
            className={`w-full text-left rounded-xl p-4 border transition-colors ${
              selected?.league_key === league.league_key
                ? 'border-purple-500 bg-purple-900/30'
                : 'border-gray-700 bg-gray-800 hover:border-gray-600'
            } disabled:opacity-60`}
          >
            <div className="flex items-center justify-between">
              <div className="font-semibold">{league.name}</div>
              {loadingKey === league.league_key && (
                <span className="text-xs text-gray-400">Loading...</span>
              )}
            </div>
            <div className="text-sm text-gray-400 mt-1">
              {league.num_teams} teams
              {league.scoring_type ? ` · ${league.scoring_type}` : ''}
              {league.draft_type ? ` · ${league.draft_type}` : ''}
              {` · ${league.season}`}
              {league.is_finished ? ' (Finished)' : ''}
            </div>
          </button>
        ))}
      </div>

      {error && <p className="text-yellow-400 text-sm mb-4">{error}</p>}

      <button
        onClick={() => onSelect(selected)}
        disabled={!selected || loadingKey !== null}
        className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white font-medium px-6 py-3 rounded-lg transition-colors"
      >
        Continue
      </button>
      <BackButton onClick={onBack} />
    </div>
  )
}

// Friendly labels for scoring/draft types
// Step 3 — Yahoo Confirm
function YahooConfirmStep({ league, onImport, onBack }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleImport = async () => {
    setError('')
    setLoading(true)
    try {
      const resp = await apiClient.post('/leagues/connect/yahoo', {
        league_id: league.league_id,
        league_key: league.league_key,
        season: parseInt(league.season),
        num_teams: league.num_teams,
        draft_type: league.draft_type,
        scoring: league.scoring_type,
        is_finished: league.is_finished || false,
      })
      onImport(resp.data)
    } catch (err) {
      const resp = err.response
      if (resp?.status === 403 && resp?.data?.error_code === 'league_limit_reached') {
        const d = resp.data
        setError(
          `You've reached your league limit (${d.current_leagues} of ${d.max_leagues}). ` +
          'Upgrade your plan to add more leagues.'
        )
      } else {
        setError(resp?.data?.message || 'Failed to import league')
      }
      setLoading(false)
    }
  }

  const scoringLabel = SCORING_LABELS[league.scoring_type] || league.scoring_type || '—'
  const draftLabel = DRAFT_LABELS[league.draft_type] || league.draft_type || '—'

  return (
    <div>
      <h2 className="text-2xl font-bold mb-2">Confirm League</h2>
      <p className="text-gray-400 mb-6">Review your league details before importing.</p>

      <div className="bg-gray-800 rounded-xl p-6 space-y-3 max-w-md mb-8">
        <SummaryRow label="League" value={league.name} />
        <SummaryRow label="Teams" value={league.num_teams} />
        <SummaryRow label="Format" value={draftLabel} />
        <SummaryRow label="Scoring" value={scoringLabel} />
        <SummaryRow label="Season" value={league.season} />
        {league.auction_budget != null && (
          <SummaryRow label="Auction Budget" value={`$${league.auction_budget}`} />
        )}
        {league.playoff_start_week && (
          <SummaryRow label="Playoffs Start" value={`Week ${league.playoff_start_week}`} />
        )}
      </div>

      {error && (
        <div className="mb-4">
          <p className="text-red-400 text-sm">{error}</p>
          {error.includes('league limit') && (
            <a
              href="/pricing"
              className="text-blue-400 hover:text-blue-300 text-sm underline mt-1 inline-block"
            >
              View upgrade options
            </a>
          )}
        </div>
      )}

      <div className="flex gap-4">
        <button
          onClick={onBack}
          className="text-gray-400 hover:text-gray-300 font-medium px-6 py-3 transition-colors"
        >
          &larr; Back
        </button>
        <button
          onClick={handleImport}
          disabled={loading}
          className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white font-medium px-6 py-3 rounded-lg transition-colors"
        >
          {loading ? 'Importing...' : 'Import League'}
        </button>
      </div>
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
        Rook ESPN Connect
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

// Step 4 — Import result (shared across platforms)
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
  const [yahooLeagues, setYahooLeagues] = useState(null)
  const [selectedLeague, setSelectedLeague] = useState(null)
  const [retryMessage, setRetryMessage] = useState('')

  // Handle redirect params (?platform=espn or ?platform=yahoo after OAuth)
  useEffect(() => {
    const p = searchParams.get('platform')
    const error = searchParams.get('error')
    const retry = searchParams.get('retry')

    if (error === 'account_not_ready' && retry === 'true') {
      setRetryMessage('Account is setting up — retrying automatically...')
      setPlatform(p || 'yahoo')
      setStep(1)
      const timer = setTimeout(() => {
        setRetryMessage('')
        window.location.href = `${API_URL}/auth/yahoo/connect`
      }, 2000)
      return () => clearTimeout(timer)
    }

    if (error === 'invalid_state') {
      setRetryMessage('OAuth session expired. Please try connecting again.')
      setPlatform(p || 'yahoo')
      setStep(1)
      return
    }

    if (p) {
      setPlatform(p)
      setStep(1)
    }
  }, [searchParams])

  const handlePlatformSelect = (id) => {
    setPlatform(id)
    setStep(1)
  }

  // ESPN/Sleeper direct connect → skip to results
  const handleConnected = (data) => {
    setResult(data)
    setStep(4)
  }

  // Yahoo OAuth complete → show league list
  const handleYahooLeagues = (leagues) => {
    setYahooLeagues(leagues)
    setStep(2)
  }

  // Yahoo league selected → confirm
  const handleLeagueSelected = (league) => {
    setSelectedLeague(league)
    setStep(3)
  }

  // Yahoo import complete → show results
  const handleYahooImport = (data) => {
    setResult(data)
    setStep(4)
  }

  const handleBack = () => {
    if (step === 1) {
      setPlatform(null)
      setStep(0)
    } else if (step === 2) {
      setStep(1)
    } else if (step === 3) {
      setStep(2)
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

        {retryMessage && (
          <div className="text-yellow-400 text-sm mb-4 bg-yellow-900/20 border border-yellow-800 rounded-lg px-4 py-3">
            {retryMessage}
          </div>
        )}

        {step === 0 && <PlatformStep onSelect={handlePlatformSelect} />}
        {step === 1 && (
          <ConnectStep
            platform={platform}
            onConnected={handleConnected}
            onYahooLeagues={handleYahooLeagues}
            onBack={handleBack}
          />
        )}
        {step === 2 && platform === 'yahoo' && (
          <YahooLeagueSelect
            leagues={yahooLeagues}
            onSelect={handleLeagueSelected}
            onBack={handleBack}
          />
        )}
        {step === 3 && platform === 'yahoo' && selectedLeague && (
          <YahooConfirmStep
            league={selectedLeague}
            onImport={handleYahooImport}
            onBack={handleBack}
          />
        )}
        {step === 4 && result && <ConfirmStep result={result} />}
      </div>
    </div>
  )
}
