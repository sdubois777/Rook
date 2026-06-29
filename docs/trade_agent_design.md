# Rook — Trade Agent + Trade Page: Design / Scoping Pass

> **Status: design only. No code in this PR.** Deliverable is this doc.
> MVP = **Trade Analyzer** (evaluate a trade you build) **+ Trade Proposals** (agent
> finds trades on its own). Built and tested **pre-season against a DUMMY league**
> (real players, "week 5 of 2025", real 2025 weekly usage), behind a clean
> league-state interface so the *same agents* later run on real in-season data.

---

## ⚠️ Test scaffolding — MUST be removed before prod (read first)

The dummy-data testing surfaces exist **only** to build/validate the agents before
the season. **None of it ships to users.** Everything test-only is gated behind a
single env flag and namespaced for one-pass deletion.

- **Master switch:** `TRADE_DEMO_MODE` env (default **false** in prod). When false,
  every demo surface below is inert/absent — the trade page operates only on the
  authed user's **real** league.
- **Removal checklist (delete before prod / when in-season data lands):**
  - `scripts/seed_demo_league.py` — thin CLI that rosters real 2025 players onto
    the demo teams and prints engine verdicts (slice 2)
  - the demo league-state source (provider + gate + roster assembly, serving the
    real 2025 per-week layer at a pinned demo week) —
    `backend/services/trade/trade_demo_source.py` (slice 2). Holds `DEMO_ROSTERS`,
    the `TRADE_DEMO_MODE` gate (`trade_demo_enabled` / `maybe_demo_league_source`),
    and the demo anchor `DEMO_SEASON = 2025` / `DEMO_CURRENT_WEEK` (pinned HERE,
    currently week 14 — NOT in the engine or #149 data layer)
  - the demo tests — `tests/unit/services/trade/test_trade_demo.py` (slice 2)
  - any `/_demo` trade endpoints + the `TRADE_DEMO_MODE` branch in route/provider
    selection (slice 3)
  - frontend **team-switcher** ("act as any team") + `TradeTestControls` component
    (slice 5)
  - grep `TRADE_DEMO` / `trade_demo` / `DEMO_ROSTERS` to find every surface
- **What is PERMANENT (the real feature, not test):** the league-state **interface**,
  the **in-season value engine**, the **analyzer** + **proposals** agents, the trade
  **page** (player selection, verdict, the two buttons — minus the team-switcher),
  `/api/trade/analyze` + `/api/trade/ideas`, and the credit/feature gates.

The split is deliberate: only the **data source** and the **test affordances** are
throwaway. The agents reason over an interface, so swapping the dummy source for real
data changes nothing about them. **A note also lives in CLAUDE.md — keep both in
sync; do not let demo code reach prod.**

---

## 1. Scope (locked with Stephen)

- **MVP:** analyzer **and** proposals (both are agent prompts over the same value data).
- **Demo league:** auto-generate 12 rosters from the real ADP/valuation pool (fixed
  seed, reproducible); **your team curatable**; harness also accepts an **imported
  roster set** (drop in real last-year league rosters).
- **Week 5, 2025**, real weekly usage.
- **Interface-first** — dummy harness and (later) real in-season data feed one agent.
- **Gating (already built):** analyzer → `require_credits("trade_analysis")` (standard+,
  10cr); proposals → pro-only (`trade_finder`, 20cr). See `core/dependencies.py`.

---

## 2. League-state interface (the boundary)

One interface, two implementations (demo now, real later). The agents + value engine
depend **only** on this — never on where the data came from.

```
LeagueState:
  week: int                     # current scoring week (5 in demo)
  season: int
  teams: [ TeamState ]          # 12 teams
TeamState:
  team_id, team_name, is_me: bool
  roster: [ RosterPlayer ]      # ~15 players, with starter slots
RosterPlayer:
  player (canonical id + name + pos + nfl_team + bye)
  value_bundle: InSeasonValue   # §3 — the whole point
```

- **Demo impl** (`trade_demo_source.py`, test-only): builds `LeagueState` from the
  seeded rosters + 2025 weeks 1-5 actuals.
- **Real impl** (later, permanent): builds the same `LeagueState` from `SeasonRoster`
  + the live league sync + the real current week. **No agent change.**

---

## 3. In-season value model — THE differentiator (do NOT just reuse projections)

Every other trade tool leans on preseason projections → name bias + stale value.
**Ours re-derives forward value from actual production + usage TRAJECTORY**, using the
preseason projection only as a weak prior to be **overridden** by what the data now
says. (Same chain-of-reasoning the draft side runs — Keenan Allen capping McConkey's
share — applied in-season.) This aligns with the signal taxonomy already in
`docs/INSEASON.md` (sell-high = "TD on low target share / snaps declining 2+ weeks";
buy-low = "recency bias suppressing value below true projection").

**Inputs, computed per player from real weekly data** (`fetch_weekly_stats(2025)` +
`fetch_snap_counts(2025)` + warehouse target share, weeks 1..current):
- **Usage trajectory** (the headline): target share, snap %, carry share, route
  participation, red-zone touches — and the **TREND** (e.g. last-2-weeks vs prior-3),
  not just the level. Rising share = buy pressure; 2+ weeks declining = sell pressure.
- **Opportunity-vs-production gap:** high volume + low output → buy (production catches
  the role); low volume + high output (esp. TD-driven on low targets) → sell (regresses).
- **Role-change context:** depth-chart moves, a teammate's injury opening volume, a
  committee consolidating, returning-from-injury snap ramp.
- **Recent form** weighted toward the last ~2-3 weeks (recency), but distinguished from
  **sustainable** form via the usage signals above.
- **Rest-of-season schedule** (defenses faced) as a modifier.

**Outputs per player:** `forward_value` (0-100, position-relative / VORP-anchored),
`value_trend` (rising/falling/stable), `buy_low` / `sell_high` flags + a one-line
*why*. **Name-bias guard:** an explicit rule + prompt instruction to down-weight
reputation when the underlying role has decayed; the value is justified by the usage
data, not the name.

**Prior source nuance (learned, slice-2 casting):** the demo sources the preseason
prior from `PlayerProfile.clean_season_baseline["ppr_points"] / 17`. Genuine 2025
rookies DO carry a baseline here (college-comp-derived), so a **null prior signals a
veteran missing a projection, NOT a rookie** — do not treat `prior is None` as "rookie"
in the value engine or downstream agents. Real rookies enter with a (low-confidence)
prior; the null-prior code path (`prior_weight 0`) is the unprofiled-veteran case.

Where this lives: a **value engine** (mostly Python signal computation; a Haiku pass
only for formatting if needed — Stage 17's model rule). The signals feed the Sonnet
trade agents as structured context.

---

## 4. Analyzer agent (Sonnet) — "analyze my trade"

`POST /api/trade/analyze` — body: `{ my_team_id, give: [player_ids], get: [player_ids] }`.
Sonnet (multi-step causal reasoning, per the model rule). Input = the two sides'
`value_bundle`s + both teams' roster construction (needs/surplus by position, starters,
byes) + week. **Output (structured):**
- `winner` + `value_delta` (magnitude, not just direction)
- `fairness` verdict (fleecing / fair / overpay)
- per-team **roster-fit** reasoning (does it fix a need / create a hole)
- **roster-slot check** — lineup legality is NOT enforced (it's a trade), but if I
  **receive more players than I give** and don't have the open roster slots,
  **flag it** and **recommend which of my players to drop** to make the trade fit.
- **counter** suggestion if lopsided
- the **why**, grounded in usage signals (not "X is projected for more points")

---

## 5. Proposals agent (Sonnet) — "give me trade ideas"

`POST /api/trade/ideas` — body: `{ my_team_id }`. Scans **my roster vs the rest of the
league** for value-asymmetric matches: my **sell-high** pieces against opponents'
**needs**, opponents' **buy-low** pieces against my needs. Constructs realistic,
roughly-fair offers (each must improve my starting lineup or depth), ranked by net
value gain + likelihood the other side says yes (roster-fit on their side). Reuses the
§4 value bundle + §3 signals; output is a list of proposed trades each with the
analyzer-style rationale (incl. the §4 roster-slot/drop note where relevant).
- **Count: 3-5, NOT forced** — return however many are genuinely good.
- **NEVER fabricate.** If there are no value-asymmetric, roster-improving trades,
  return **none** with a plain "no clear trade right now" — do not pad to a number or
  surface forced/marginal offers. (Same no-making-things-up discipline as the rest of
  the system.)
- (In prod, gated to **pro** via `trade_finder`.)

---

## 6. Dummy harness (test-only — see teardown)

- **Seeder** (`scripts/seed_demo_league.py`): 12 teams, snake-or-auction-realistic
  rosters drafted from the ADP/valuation pool (fixed seed). My team curatable; accepts
  an imported roster list (real last-year rosters).
- **Data:** for each rostered player, pull **2025 weeks 1-5** real production + usage
  (`fetch_weekly_stats` / `fetch_snap_counts` / warehouse target share) → the §3 value
  bundle. "Current week" hardcoded to 5 (test-only).
- Serves the §2 `LeagueState` via `trade_demo_source.py` when `TRADE_DEMO_MODE=true`.

---

## 7. Testing UX (test-only affordances)

On the trade page, gated behind `TRADE_DEMO_MODE`:
- **Team switcher** — act as any of the 12 demo teams; the agents reason against that
  team's roster vs the rest of the league. (Removed in prod — a real user is only ever
  their own team.)
- **Two buttons:**
  - **"Give me trade ideas"** → `/api/trade/ideas` (proposals agent).
  - **"Analyze my trade"** → build a give/get from the two rosters → `/api/trade/analyze`.
- Verdict panel renders the structured agent output.
- The **permanent** page = the same minus the team-switcher (your team is implicit) and
  minus the demo data.

---

## 8. Build sequence (next pass — each a PR through develop)

1. **League-state interface** + the **in-season value engine** (§2-3) — the core,
   permanent. Unit-tested on fixed 2025 weekly fixtures.
2. **Dummy harness** (§6, test-only, flag-gated) — seeder + demo source feeding the
   interface.
3. **Analyzer agent + `/api/trade/analyze`** (§4) — gated; tested against the demo league.
4. **Proposals agent + `/api/trade/ideas`** (§5) — gated.
5. **Trade page** (§7) — team-switcher + two buttons (demo), verdict panel.
6. **Teardown** (when validated / in-season data lands): delete the §0 checklist; wire
   the real league-state impl behind the same interface.

---

## 9. Resolved (LOCKED with Stephen)

- **Lineup legality:** NOT enforced (it's a trade). The only roster guard: if I
  **receive more players than I give** and lack open slots, **flag it + recommend
  which player(s) to drop** to fit (§4).
- **Number of proposals:** **3-5, not forced** — return however many are genuinely
  good, and **NONE** (with "no clear trade right now") if nothing is viable. **Never
  fabricate** to hit a count (§5).
- **Value scale:** **reuse the draft-side anchoring** (0-100 / VORP-tier) for v1, and
  iterate from there.

Design is complete — ready to build (sequence in §8).
