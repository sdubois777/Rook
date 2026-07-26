# Rook — Fantasy Football AI Platform — Claude Code Entry Point

This file is read automatically at the start of every session.
Read it fully before writing any code. (Branding/repo are all **Rook**. The Railway
service hostname remains `fantasymanager-production.up.railway.app` — the extension's
hardcoded API base; it works, intentionally left as-is.)

---

## Development Workflow (enforced by CI)

All changes must go through PRs. Direct pushes to `main` and `develop` are
blocked by a branch-protection ruleset that requires the `backend`, `frontend`,
and `extension` CI checks to pass (and the branch to be up to date).

```
git checkout develop
git pull origin develop
git checkout -b feature/your-change
# ... make changes, commit ...
git push origin feature/your-change
gh pr create --base develop --fill
# Wait for CI green, then:
gh pr merge --squash --delete-branch
```

**Releasing develop → main (reconcile-branch — IMPORTANT).** A direct
`develop → main` PR comes up `BEHIND` (main carries squash/merge release commits
develop lacks), and develop's branch protection **blocks `gh pr update-branch`**.
So releases use a reconcile branch whose tree equals develop's:

```
git fetch origin main develop
git checkout -B release-NN origin/main
git merge origin/develop -X theirs --no-edit       # develop is authoritative
git diff origin/develop release-NN --stat          # MUST be empty (tree == develop)
git push -u origin release-NN
gh pr create --base main --head release-NN --title "Release: ... ; develop authoritative"
# CI green → MERGE COMMIT (not squash), then verify:
git diff origin/main origin/develop --stat         # MUST be empty
```

- **Stephen drives every `release → main` manually** — never auto-release; a bug
  report or a "fix it" is NOT a release go-ahead.
- Backend changes do **nothing in prod until released** (Railway deploys from main).
  Extension changes take effect on local rebuild + reload — no release needed to
  *test*, but they're not live for users until shipped.
- Keep unrelated working-tree files out of commits. Known junk artifact:
  `frontend/public/android-chrome-192x192.png` keeps reappearing —
  `git checkout --` it, never commit. Also `docs/PROJECT_STATE.md` (untracked audit).

---

## What This Project Is

**Rook** (rookff.com) — a full-season fantasy football management SaaS powered by
AI agents. Multi-user, multi-platform: **Yahoo, ESPN, and Sleeper**, both **auction
and snake** draft formats (all four platform×format combinations live in production
as of June 2026).

Three phases:
1. **Pre-draft pipeline** — 6 research agents build a structured "draft bible"
2. **Live draft** — a **sideloaded browser extension** reads the draft room and
   relays events to the backend, which gives real-time AI recommendations. One
   poller per platform/format; all map onto a single backend event contract. (The
   old Playwright bridge is superseded — see the Live-Draft Extension section.)
3. **In-season** — Trade analyzer, lineup optimizer, waiver wire agent (not yet built)

Core philosophy: **never trust third-party projections**. Build valuations from raw data and chain-of-reasoning. The canonical failure case this system exists to prevent: Keenan Allen signing with LAC should have automatically flagged Ladd McConkey's target share as capped. It didn't in 2024. It must in this system.

Monetization: free-to-play-style **subscription tiers** (intro/standard/pro) gated by
`User.tier`; Stripe billing is **designed but not yet implemented** — see
`docs/stripe_billing_design.md`.

---

## Mandatory Reading Before Writing Any Code

| Task | Read first |
|------|-----------|
| Any agent | `docs/rules/COST_RULES.md` + `docs/rules/PATTERNS.md` |
| Any agent | `docs/AGENTS.md` for that agent's spec |
| Database work | the ORM models in `backend/models/` (there is NO docs/SCHEMA.md) |
| Live-draft extension (any platform) | the "Live Draft — Browser Extension Architecture" section below |
| ESPN / Sleeper resolvers | `docs/espn_resolver_design.md` · `docs/sleeper_resolver_design.md` |
| Stripe / billing | `docs/stripe_billing_design.md` (decisions locked) |
| Trade agent / acceptability model | `docs/trade_agent_design.md` · `docs/trade_acceptability_design.md` (locked) |
| Trade value / lineup objective | `docs/trade_value_trajectory_design.md` · `docs/trade_lineup_value_design.md` · `docs/trade_value_availability_design.md` (locked) |
| Testing/commits | `docs/rules/GIT_RULES.md` |
| In-season features | `docs/INSEASON.md` |
| Current stage | `docs/stages/stage-XX-name.md` |
| Bid ceilings, live draft, valuations, lineup optimizer | `docs/rules/LEAGUE_RULES.md` |
| App design and UI | `docs/APP_DESIGN.md` |
| Data sources | `backend/integrations/sleeper.py` + `backend/integrations/nfl_data.py` |

---

## Model Selection — Non-Negotiable

**Haiku** (`claude-haiku-4-5-20251001`) for:
- Team Systems agent
- Injury Risk agent
- Schedule agent
- Beat Reporter agent
- Team Notes agent
- Waiver Wire agent
- Any data extraction or formatting task

**MIXED** — these run BOTH models and the split is deliberate:
- **Player Profiles** — Haiku for the per-team batch, plus a per-player **Sonnet** call
  (800 tok) for complex players. Previously listed as Haiku-only, which understated its
  cost by roughly an order of magnitude: this is the single most expensive stage.
- **Valuation Agent** — tier-batched. T1 per-player Sonnet, T2-3 batches of 5 Sonnet,
  T4-5 batches of 10 Haiku.

**Sonnet** (`claude-sonnet-4-6`) for:
- Roster Changes agent (chain-of-reasoning)
- Live Draft agent (real-time decisions)
- Trade Analyzer engine
- Trade Proposal engine
- Opponent Analyzer agent
- Any multi-step causal reasoning task

If you are unsure which to use: **default to Haiku**. Upgrade to Sonnet only if the task requires reasoning through cause-and-effect chains, not just retrieving and formatting data.

---

## Season Year Handling — Critical

**Never hardcode season years.** All agents must derive years dynamically from `backend/utils/seasons.py`:

```python
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

CURRENT_SEASON   = get_current_season()      # e.g. 2026 in May 2026
ANALYSIS_SEASONS = get_analysis_seasons(3)   # e.g. [2023, 2024, 2025] — last 3 completed
ANALYSIS_YEAR    = get_analysis_year()       # e.g. 2026 — season we're drafting FOR
```

**Season calendar logic (cutoff = March):**
- January/February → current season = prior year (playoffs still in progress)
- March onward → current season = this calendar year (new league year begins)
- `get_analysis_seasons(3)` always returns 3 **completed** seasons, never the current year
- `get_analysis_year()` = `get_current_season()` — the season being drafted for
- Backtest default = `get_current_season() - 1` — most recently completed season

If you see `CURRENT_SEASON = 2024` or `for season in [2022, 2023, 2024]` anywhere in the codebase, that is a bug. Fix it.

**AS-OF RUNS (`ROOK_ASOF_DATE`).** Set `ROOK_ASOF_DATE=YYYY-MM-DD` to make the whole
system behave as if it were that date — used to rebuild a board for a past preseason
(prospective backtesting). Unset means real time, so forgetting keeps you in the present;
a malformed value raises rather than silently falling back.

```
ROOK_ASOF_DATE=2025-08-15  →  current=2025  analysis=[2022,2023,2024]  latest_with_data=2024
unset                      →  current=2026  analysis=[2023,2024,2025]  latest_with_data=2025
```

It is an **env var, not a parameter**, because the pipeline shells out to `seed`,
`sync_rosters` and `sync_adp` via `subprocess.run` — an in-process override would not
reach the three stages that write `players.team_abbr` and `depth_chart_order`.

If you add a new clock read, derive it from `asof_date()` / `asof_now()` in
`backend/utils/seasons.py`, never `date.today()` or `datetime.now()`. Both must move
together: `latest_season_with_data()` ceilings at `get_current_season()` but PROBES via
`get_current_nfl_week()`, so a date-only override would return the very season being
projected — and `team_metrics` **writes** that value to `team_systems`.

**Rolling the clock back does NOT roll back roster state.** Sleeper serves current data
only, so an as-of run still needs 2025-vintage rosters/depth charts from nflverse.
Measured: 138 of 539 valued players changed teams between the 2025 roster and today.

---

## Architecture Rules (Full Detail in docs/rules/PATTERNS.md)

These are **design intent**. Several are currently violated by shipped code. Each is
marked with its real status as of July 2026 so you can tell "this is the rule, follow it
in new code" from "this is already true everywhere". Do not assume a rule holds because
it is listed here — that assumption has cost real debugging time.

1. **One API call per team** — pre-aggregate in Python first, then call the model once.
   ⚠️ **VIOLATED by `player_profiles`**, which additionally makes a per-player Sonnet call
   for complex players (`call_once` at player_profiles.py:1932). Measured: 584 of 653
   valued players took a per-player call. This is both the dominant pipeline cost and the
   source of the bucketed projections. Hold the line in new agents.
2. **No iterative tool-use loops** in pre-draft pipeline agents — `run_agent()` is only for
   live draft. ✅ Holds.
3. **No polling** anywhere in the live draft event chain — event-driven only.
   Sleeper's primary path is a WebSocket interception; some poll intervals exist as gap
   recovery. Verify per platform before relying on this.
4. **All agents go through BaseAgent** — never call `client.messages.create()` directly.
   ⚠️ **VIOLATED by `team_notes.py:81`**, a real generation call that bypasses BaseAgent
   (so it is uncached, unlogged in `api_usage_log`, and unmetered).
   Two sanctioned exceptions: `agent_loop.py:51` (live draft, explicitly carved out by
   rule 2) and `player_profiles.py:857` (a 10-token "respond with OK" Sonnet health ping,
   not a generation).
5. **Batch by team, never by player** — never loop over players calling the API inside the
   loop. ⚠️ **VIOLATED by `player_profiles`** — same path as rule 1.
   `valuation_agent` is NOT a violation: it is tier-batched (T1 per-player Sonnet, T2-3
   batches of 5 Sonnet, T4-5 batches of 10 Haiku).
6. **All data flows through NflDataWarehouse** — agents never fetch independently.
   ✅ The literal test passes: `grep _data_cache backend/agents/` returns zero.
   ⚠️ But the spirit is violated — `team_systems` calls `import_draft_picks` directly, and
   `roster_changes` pulls OverTheCap / college / comp data outside the warehouse. Also
   note `valuation._load_prior_production()` builds a **second** warehouse.
7. **Player identity uses ID-first matching** — always match by
   `sleeper_id` → `sportradar_id` → `gsis_id` → full name + position.
   Never match by last name alone. Never cross positions.
   ⚠️ Note the name fallback is genuinely unsafe: first-initial+surname collides
   (`Jonathan Taylor` and `J'mari Taylor` both key to `jtaylor`; `Frank Gore Sr` and
   `Frank Gore Jr` are same-name/same-position different humans). There are ~54 duplicate
   player-row clusters in the DB from exactly this. Never dedupe on a name key.
8. **Sleeper is the primary data source** for player identity, rosters, depth charts,
   injuries, and season stats. nfl_data_py kept only for schedules, PBP, and NGS.
   ⚠️ Partly aspirational: `get_seasonal_stats` (nfl_data_py) is what the warehouse
   actually uses; `get_sleeper_seasonal_stats` has no non-test callers.
   **Sleeper serves CURRENT state only** — it has no historical rosters or depth charts,
   which is why an as-of rebuild needs nflverse for those.

---

## Data Sources

### Sleeper API — PRIMARY (`backend/integrations/sleeper.py`)
Free public API, no auth required, updated daily. Always current.

| Data | Function | Replaces |
|------|----------|---------|
| Current rosters + team assignments | `fetch_sleeper_players()` | `fetch_rosters()` |
| Season stats (pts_ppr, gp, rec, rush) | `get_sleeper_seasonal_stats(season)` | `get_seasonal_stats()` |
| Depth charts (depth_chart_order) | `get_sleeper_depth_charts()` | `fetch_depth_charts()` |
| Injury status | `get_sleeper_injuries()` | `fetch_injuries()` |

Key facts:
- 3,936 active skill position players (includes Inactive/IR)
- `sportradar_id` at 98%+ coverage — primary cross-source ID
- `depth_chart_order=1` reliably identifies starters
- Correctly shows Rodgers as FA, Geno Smith at NYJ depth=1
- Cache TTL: 24h for current data, permanent for historical seasons

### nfl_data_py — SECONDARY (`backend/integrations/nfl_data.py`)
Kept only for data Sleeper doesn't provide:

| Data | Function | Why kept |
|------|----------|---------|
| NFL schedules | `fetch_schedules(season)` | No Sleeper equivalent |
| Oline sack rates | `compute_team_oline_stats(season)` | Needs PBP pass_attempt/sack |
| NGS metrics | `fetch_ngs_data(stat_type, season)` | CPOE, air yards, time-to-throw |

**CRITICAL — never call `import_pbp_data()` with `columns=` kwarg.**
Triggers `KeyError: 'game_id'` for 2025 data due to nflverse schema change.
Load full PBP, then slice the columns you need afterward.

### Known Data Gaps
- `player_stats_{year}.parquet` on nflverse publishes 2-3 months after season ends.
  Sleeper fills this gap natively — 2025 stats available immediately.
- Full pipeline refresh should run in late July when training camp data publishes.
- nfl_data_py depth chart feed has stale entries — use Sleeper depth charts only.

---

## Player Identity — IDs on players Table

Three ID columns, all indexed:
- `sleeper_id` — Sleeper's own ID, 100% for active players
- `sportradar_id` — 98%+ coverage, most reliable cross-source ID
- `gsis_id` — 29% coverage from Sleeper (supplemented from nfl_data_py)

Stat lookup priority in `_get_player_season_stats()`:
1. `sleeper_id` exact match (best)
2. `sportradar_id` exact match
3. `gsis_id` exact match
4. Full name + position match (Sleeper names are reliable)
5. Return `{}` — never return wrong-player stats

---

## Pipeline Dependency Order (CRITICAL)

**The authoritative order is `PHASES` in `scripts/run_predraft_pipeline.py`.** Read it
there — this block is a summary and can go stale. `PIPELINE_ORDER` is DERIVED from
`PHASES` (a flatten), so the two cannot drift; do not hand-edit it.

Three stages run **unconditionally as subprocesses before any agent**, regardless of
`--agent`. They are not in `PHASES` and `--dry-run` skips them entirely:

```
seed         (scripts/seed_nfl_data.py)  → upserts players, writes data/cache/*.parquet
sync_rosters (scripts/sync_rosters.py)   → CURRENT Sleeper state onto players;
                                            DESTRUCTIVE: deletes agent_cache rows for
                                            teams whose roster changed
sync_adp     (scripts/sync_adp.py)       → live Playwright FantasyPros ADP scrape
then: NflDataWarehouse.build()           → 6 seasons, built once, passed to every agent
```

Because these are **subprocesses**, an in-process override does not reach them. That is
why the as-of clock is an env var (`ROOK_ASOF_DATE`) — see backend/utils/seasons.py.

Then the agent phases (inner lists run in parallel):

```
1.  team_systems       ← identity + inputs (QB id, sack_rate, rookie flag) — NOT grades
1b. team_metrics       ← deterministic grades + composite; the SOLE grade owner. Pure
                         Python. Runs EARLY (roster_changes and player_profiles read it)
2.  roster_changes     ← needs team_systems + the grades above
3.  injury_risk | schedule | beat_reporter   ← independent, run in parallel
4.  player_profiles    ← needs all above; synthesizes them
4b. kicker_baseline    ← K prior (pure Python)
4c. defense_baseline   ← DST prior (pure Python)
5.  valuation          ← needs profiles. Pure Python, no API calls
6.  valuation_agent    ← needs valuation
6b. format_market      ← per-format ADP + auction re-scrape
6c. team_notes         ← narrates from the stored grades/stats
7.  availability       ← LAST: deterministic games-missed discount
```

`team_metrics` at 1b and `team_notes` at 6c are the two most commonly mis-remembered —
an earlier version of this file listed them 12th and 13th, which was wrong.

---

## When to Ask the User

Stop and ask the user before:
- Any step requiring account creation (Railway, Yahoo Developer, GitHub)
- Any OAuth flow that requires browser interaction
- Any step requiring API keys or credentials not already in `.env`
- Running the full pipeline for the first time (show dry-run cost estimate first)
- Any destructive database operation

---

## Repository Layout, Status & Directory-Specific Guidance — Lazy-Loaded

To keep this always-loaded entry point lean, detailed and directory-specific
guidance now lives in files that load only when relevant:

- **Project status, pipeline state, backtest results, and the full backlog** →
  `docs/STATUS.md` (read it for current stage, test counts, or open issues).
- **Live-draft browser-extension architecture + extension backlog** →
  `extension/CLAUDE.md` (auto-loads when working under `extension/`).
- **Frontend responsive / mobile conventions** → `frontend/CLAUDE.md`
  (auto-loads when working under `frontend/`).

The repository-structure tree, the tech-stack table, and the `NflDataWarehouse`
accessor list were removed as derivable from the codebase (`ls`, the package
manifests, and the class source). Everything above is preserved in git history
and in the lazy-loaded files.

---

## SaaS Pricing (Stages 25-30)

**THE ONLY definition of tiers, prices, credit costs, grants, and packs is
`backend/models/user.py`** (`TIER_LIMITS` / `CREDIT_COSTS` / `CREDIT_PACKS` /
`TIER_ORDER`). Do NOT restate the numbers here or anywhere else — restated
numbers are exactly how the old four-way pricing drift happened. Consumers
derive: the Stripe seeder (`scripts/stripe_seed_test.py`), the billing catalog,
the public `GET /billing/pricing` endpoint (which the frontend — PricingTable,
credit labels, pack cards — renders from via `usePricing`), and the docs.

Model shape (semantics, not numbers): FREE tier = metered (every AI feature
costs credits; one-time signup grant); PAID tiers (standard/pro, monthly sub or
one-time SEASON pass with `users.tier_expires_at`) = unlimited, no credits.
Live draft is a tier ENTITLEMENT (standard+), never credit-metered. Always free
for everyone: player values, teams, player detail, waiver wire browse,
start/sit, injury revaluation (pipeline-shared — gating it would serve stale
values). Credits carry over; no monthly credit grants exist.

**Stripe billing: BUILT** (test mode; go-live blocked only on the business bank
account). Checkout (tier monthly/season + credit pack), change-plan w/ proration
(monthly<->monthly; season passes are purchases, not plan changes), portal,
signature-verified webhook entitlements (incl. season expiry + monthly-sub
cancel-at-period-end on season purchase), league-cap reconciliation, test-mode
seeder deriving all amounts from user.py. Design doc: `docs/stripe_billing_design.md`
(decision #3 UPDATED — seasonal is IN as a one-time entitlement).

---

## gstack

gstack is installed globally at `~/.claude/skills/gstack`.

**Web browsing — non-negotiable.** Use the **`/browse`** skill from gstack for
ALL web browsing. **Never** use the `mcp__claude-in-chrome__*` tools.

**Available skills:**

`/office-hours` · `/plan-ceo-review` · `/plan-eng-review` · `/plan-design-review` ·
`/design-consultation` · `/design-shotgun` · `/design-html` · `/review` · `/ship` ·
`/land-and-deploy` · `/canary` · `/benchmark` · `/browse` · `/connect-chrome` ·
`/qa` · `/qa-only` · `/design-review` · `/setup-browser-cookies` · `/setup-deploy` ·
`/setup-gbrain` · `/retro` · `/investigate` · `/document-release` ·
`/document-generate` · `/codex` · `/cso` · `/autoplan` · `/plan-devex-review` ·
`/devex-review` · `/careful` · `/freeze` · `/guard` · `/unfreeze` ·
`/gstack-upgrade` · `/learn`
