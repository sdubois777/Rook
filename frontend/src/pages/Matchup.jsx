/**
 * Matchup page — H2H league-opponent scouting (GET /matchup/league).
 *
 * ZERO-METERED-COST surface: every number is a pure/deterministic primitive on the
 * SAME evaluate_league basis as the Trade/Waiver pages (numbers match across pages).
 * The ONLY path to a paid call is the explicit "Explore a trade" handoff, which
 * navigates to the Trade Build tab pre-seeded with the scouted opponent (?opponent=)
 * — it never runs the finder/analyzer itself.
 *
 * The "Acting as" perspective switch is DEMO-ONLY scaffolding (TRADE_DEMO_MODE);
 * the rest of the page is permanent.
 */
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Swords, ArrowLeftRight, Trophy, Scale } from 'lucide-react'
import { fetchMatchupLeague } from '../api/matchup'

const POS_CLS = {
  QB: 'bg-rose-500/15 text-rose-300', RB: 'bg-emerald-500/15 text-emerald-300',
  WR: 'bg-sky-500/15 text-sky-300', TE: 'bg-amber-500/15 text-amber-300',
  K: 'bg-violet-500/15 text-violet-300', DEF: 'bg-cyan-500/15 text-cyan-300',
}

function PosBadge({ pos }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${POS_CLS[pos] || 'bg-slate-500/15 text-slate-300'}`}>
      {pos}
    </span>
  )
}

// Approximate qualitative edge — margin is the headline; the band is deliberately
// NOT a calibrated %, because no per-player variance exists (honesty rule).
function bandClass(band) {
  if (band.startsWith('Heavy favorite') || band === 'Favored') return 'text-emerald-300 border-emerald-500/30 bg-emerald-500/5'
  if (band.startsWith('Slight edge')) return 'text-emerald-200 border-emerald-500/20 bg-emerald-500/5'
  if (band === 'Toss-up') return 'text-slate-300 border-border bg-surface-2'
  if (band.startsWith('Slight underdog')) return 'text-amber-200 border-amber-500/20 bg-amber-500/5'
  return 'text-amber-300 border-amber-500/30 bg-amber-500/5' // Underdog / Heavy underdog
}

function Chips({ positions, accent }) {
  if (!positions?.length) return <span className="text-slate-600">—</span>
  return (
    <span className="inline-flex flex-wrap gap-1">
      {positions.map((p) => (
        <span key={p} className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${accent}`}>{p}</span>
      ))}
    </span>
  )
}

// --- Weekly H2H preview (margin prominent, approximate band) ---------------
function H2HPreview({ me, scout }) {
  const iLead = scout.margin >= 0
  return (
    <section className="rounded-lg border border-border bg-surface-1 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
        <Swords size={16} className="text-brand-accent" /> This week
      </div>
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
        <div className="text-right">
          <div className="truncate text-sm font-medium text-white">{me}</div>
          <div className="text-2xl font-bold tabular-nums text-white">{scout.my_ppw.toFixed(1)}</div>
          <div className="text-[11px] text-slate-500">proj. pts/wk</div>
        </div>
        <div className="flex flex-col items-center gap-1">
          <span className={`rounded-md border px-2.5 py-1 text-xs font-semibold ${bandClass(scout.win_prob_band)}`}>
            {scout.win_prob_band}
          </span>
          <span className="text-[11px] tabular-nums text-slate-400">
            {iLead ? '+' : ''}{scout.margin.toFixed(1)} margin
          </span>
        </div>
        <div className="text-left">
          <div className="truncate text-sm font-medium text-white">{scout.opponent_team_name}</div>
          <div className="text-2xl font-bold tabular-nums text-white">{scout.opp_ppw.toFixed(1)}</div>
          <div className="text-[11px] text-slate-500">proj. pts/wk</div>
        </div>
      </div>
      <p className="mt-3 text-center text-[11px] text-slate-500">
        Approximate edge from the projected margin — not a calibrated probability
        {scout.confidence_note !== 'full' ? ` · confidence: ${scout.confidence_note.replace('_', ' ')}` : ''}
      </p>
    </section>
  )
}

// --- Positional battle grid (by optimal-lineup slot) -----------------------
function BattleGrid({ me, scout }) {
  const max = Math.max(1, ...scout.grid.flatMap((g) => [g.mine, g.theirs]))
  return (
    <section className="rounded-lg border border-border bg-surface-1 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
        <Scale size={16} className="text-brand-accent" /> Positional battle
        <span className="ml-auto text-[11px] font-normal text-slate-500">startable pts/wk by slot</span>
      </div>
      <div className="space-y-1.5">
        <div className="grid grid-cols-[1fr_auto_1fr] gap-2 text-[11px] text-slate-500">
          <div className="text-right">{me}</div><div className="text-center">pos</div><div>{scout.opponent_team_name}</div>
        </div>
        {scout.grid.map((g) => {
          const iWin = g.mine >= g.theirs
          return (
            <div key={g.position} className="grid grid-cols-[1fr_auto_1fr] items-center gap-2">
              <div className="flex items-center justify-end gap-2">
                <span className={`text-sm tabular-nums ${iWin ? 'font-semibold text-emerald-300' : 'text-slate-400'}`}>{g.mine.toFixed(1)}</span>
                <div className="h-2 w-full max-w-[7rem] overflow-hidden rounded-full bg-surface-2">
                  <div className="ml-auto h-full rounded-full bg-emerald-500/40" style={{ width: `${(g.mine / max) * 100}%` }} />
                </div>
              </div>
              <div className="w-10 text-center"><PosBadge pos={g.position} /></div>
              <div className="flex items-center gap-2">
                <div className="h-2 w-full max-w-[7rem] overflow-hidden rounded-full bg-surface-2">
                  <div className="h-full rounded-full bg-sky-500/40" style={{ width: `${(g.theirs / max) * 100}%` }} />
                </div>
                <span className={`text-sm tabular-nums ${!iWin ? 'font-semibold text-sky-300' : 'text-slate-400'}`}>{g.theirs.toFixed(1)}</span>
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// --- Leverage readout + trade handoff (funnel top, non-metered) ------------
// Surplus is VALUE-GATED (real tradeable depth, not bench headcount) and the mirror
// fires only on a genuine RECIPROCAL fit — so "you can spare X" names positions worth
// moving, and "mirror images" MEANS something (it fires rarely). No fit → say so.
function Leverage({ scout, onExplore }) {
  const fit = scout.is_reciprocal_fit
  return (
    <section className="rounded-lg border border-border bg-surface-1 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
        <ArrowLeftRight size={16} className="text-brand-accent" /> Trade leverage
      </div>
      <div className="grid gap-2 text-sm sm:grid-cols-2">
        <div className="space-y-1.5">
          <div className="text-[11px] uppercase tracking-wide text-slate-500">They’re thin at (your depth)</div>
          <div className="flex items-center gap-2">
            <span className="text-slate-400">their needs</span>
            <Chips positions={scout.opp_needs} accent="bg-sky-500/15 text-sky-300" />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-slate-400">you can spare</span>
            <Chips positions={scout.my_surplus_their_needs} accent="bg-emerald-500/20 text-emerald-300" />
          </div>
        </div>
        <div className="space-y-1.5">
          <div className="text-[11px] uppercase tracking-wide text-slate-500">You’re thin at (their depth)</div>
          <div className="flex items-center gap-2">
            <span className="text-slate-400">your needs</span>
            <Chips positions={scout.my_needs} accent="bg-emerald-500/15 text-emerald-300" />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-slate-400">they can spare</span>
            <Chips positions={scout.their_surplus_my_needs} accent="bg-sky-500/20 text-sky-300" />
          </div>
        </div>
      </div>
      <p className={`mt-3 text-xs ${fit ? 'text-emerald-300' : 'text-slate-500'}`}>
        {fit
          ? 'You’re mirror images — each side has real depth the other needs, so a fair swap helps both.'
          : 'No clean two-way fit this week — no reciprocal depth-for-need match, so a balanced swap is unlikely.'}
      </p>
      <button
        type="button"
        onClick={onExplore}
        className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-brand-accent/40 bg-brand/10 px-3 py-1.5 text-sm font-medium text-brand-accent transition-colors hover:bg-brand/20"
      >
        <ArrowLeftRight size={14} /> Explore a trade with {scout.opponent_team_name}
      </button>
    </section>
  )
}

// --- Season-long roster-strength ladder ------------------------------------
function StrengthLadder({ teams, myId }) {
  const max = Math.max(1, ...teams.map((t) => t.strength))
  return (
    <section className="rounded-lg border border-border bg-surface-1 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-white">
        <Trophy size={16} className="text-brand-accent" /> League strength ladder
      </div>
      <div className="space-y-1">
        {teams.map((t, i) => (
          <div key={t.team_id} className={`grid grid-cols-[1.5rem_1fr_3rem] items-center gap-2 rounded px-1.5 py-1 ${t.team_id === myId ? 'bg-brand/10' : ''}`}>
            <span className="text-right text-xs tabular-nums text-slate-500">{i + 1}</span>
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className={`truncate text-sm ${t.team_id === myId ? 'font-semibold text-white' : 'text-slate-300'}`}>{t.team_name}</span>
                {t.is_me && <span className="rounded bg-brand/20 px-1 text-[10px] font-semibold text-brand-accent">you</span>}
              </div>
              <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-surface-2">
                <div className="h-full rounded-full bg-brand-accent/50" style={{ width: `${(t.strength / max) * 100}%` }} />
              </div>
            </div>
            <span className="text-right text-sm font-medium tabular-nums text-white">{t.strength.toFixed(0)}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

export default function Matchup() {
  const navigate = useNavigate()
  const [myTeamId, setMyTeamId] = useState(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ['matchup-league', myTeamId],
    queryFn: () => fetchMatchupLeague({ myTeamId }),
    retry: false,
  })

  const effMyId = myTeamId || data?.my_team_id
  const meName = useMemo(
    () => data?.teams?.find((t) => t.team_id === effMyId)?.team_name || data?.my_team_name,
    [data, effMyId],
  )

  if (isLoading) return <div className="p-6 text-slate-400">Loading matchup…</div>
  if (error) {
    const demoOff = error?.response?.status === 404
    return (
      <div className="mx-auto max-w-2xl p-6">
        <div className="rounded-lg border border-border bg-surface-1 p-6 text-slate-300">
          <h1 className="mb-2 flex items-center gap-2 text-lg font-semibold text-white">
            <Swords size={20} /> Matchup
          </h1>
          {demoOff
            ? 'The matchup demo league is only available with TRADE_DEMO_MODE enabled.'
            : 'Could not load the matchup.'}
        </div>
      </div>
    )
  }

  const scout = data.scout

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 lg:p-6">
      {/* Header + demo perspective switch */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-white">
            <Swords size={22} className="text-brand-accent" /> Matchup
          </h1>
          <p className="text-sm text-slate-500">
            Week {data.week}, {data.season} · scout your opponent · free — no credits
          </p>
        </div>
        {data.demo_mode && (
          <label className="flex items-center gap-2 text-sm text-slate-400">
            Acting as
            <select
              value={effMyId}
              onChange={(e) => setMyTeamId(e.target.value)}
              className="min-h-9 rounded-md border border-border bg-surface-2 px-2 py-1 text-white"
            >
              {data.teams.map((t) => (
                <option key={t.team_id} value={t.team_id}>{t.team_name}{t.is_me ? ' (you)' : ''}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      {scout ? (
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-4 lg:col-span-2">
            <H2HPreview me={meName} scout={scout} />
            <BattleGrid me={meName} scout={scout} />
            <Leverage
              scout={scout}
              onExplore={() => navigate(`/trade?opponent=${encodeURIComponent(scout.opponent_team_id)}`)}
            />
          </div>
          <StrengthLadder teams={data.teams} myId={effMyId} />
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="rounded-lg border border-border bg-surface-1 p-6 text-slate-400 lg:col-span-2">
            {meName} has a bye this week — no opponent to scout.
          </div>
          <StrengthLadder teams={data.teams} myId={effMyId} />
        </div>
      )}
    </div>
  )
}
