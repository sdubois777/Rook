# Handoff — as-of 2025 prospective backtest

Context for resuming. Written 2026-07-25. Everything below is verified unless marked otherwise.

---

## ✅ COMPLETE — 2026-07-25

All five steps done. The as-of board no longer exists (2026 restored).

1. Pipeline finished — 838 players valued, 973 profiles, 513 dependency flags.
2. **Backtest run. Result: 56.9% signal accuracy on 65 calls, p=0.161 — NOT
   distinguishable from the ~50% base rate.** 95% CI 44.8–68.2%. Top opportunities
   landed at exactly 50.0% (14/28). Full write-up, including the diagnosis, in
   **`docs/recon/asof_2025_backtest_result.md`**; per-player rows preserved in
   `docs/recon/asof_2025_backtest_rows.csv` (the only surviving copy — the board is gone).
3. Pipeline changes merged as **#377** (as-of guards for `sync_adp` / `format_market`,
   plus `_seed_asof_market`), with 5 mutation-verified tests.
   `trade_demo_source.py` deliberately left uncommitted.
4. Reported honestly — see below and the recon doc.
5. 2026 board restored and verified (A.J. Brown → NE, 4026 players, 29 tables).

**The restore command in this doc was WRONG and has been corrected below** — the dump has
no DROP/TRUNCATE, so piping it into a populated DB appends instead of replacing.

### The headline finding

The projection layer is compressed, with a clean monotone bias gradient: top-5 finishers
are under-projected by **−76 PPR**, rank-49+ over-projected by **+31**. Actual-to-projected
spread ratio is 1.44× at WR, 1.38× at RB. That makes every expensive player look
overpriced, so the system said *avoid* on Puka Nacua ($43 → 375 PPR), CMC ($50 → 415),
Trey McBride ($39 → 316) and Amon-Ra St. Brown ($51 → 324).

Worse: `corr(ai_ceiling − price, actual − price_implied) = 0.057`. The quantity every
signal is built from is uncorrelated with the outcome. And within position the market
out-predicts us — RB 0.719 vs our 0.645, WR 0.414 vs our 0.378 — while
`corr(ai_ceiling, league_price) = 0.759`, i.e. three-quarters of our ceiling *is* the
market it is meant to beat.

---

## Where things stand

An as-of 2025 board is being rebuilt on the **dev** DB (`localhost:5433/rook`) so the
system can be backtested prospectively — projections that never saw 2025, scored against
2025 actuals and the real 2025 auction.

**Pipeline state:** the full as-of run was in `valuation_agent` (the last stage, ~45 min)
when this was written. Everything before it completed. Check with:

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe -c "
import asyncio,logging; logging.disable(logging.CRITICAL)
from sqlalchemy import text
from backend.database import AsyncSessionLocal
async def m():
    async with AsyncSessionLocal() as s:
        for t in ['team_systems','player_dependencies','player_profiles','player_format_values']:
            print(t, (await s.execute(text(f'SELECT count(*) FROM {t}'))).scalar())
        print('valued', (await s.execute(text('SELECT count(*) FROM players WHERE ai_bid_ceiling IS NOT NULL'))).scalar())
asyncio.run(m())"
```

If `players.ai_bid_ceiling` is populated for ~800 players, `valuation_agent` finished.

---

## THE IMMEDIATE NEXT STEP

Run the prospective backtest and report it:

```bash
PYTHONUTF8=1 .venv/Scripts/python.exe -c "
import asyncio,logging,json; logging.disable(logging.CRITICAL)
from backend.database import AsyncSessionLocal
from backend.engines.backtest import run_backtest
async def m():
    async with AsyncSessionLocal() as s:
        metrics, df = await run_backtest(s, 2025)
    print(json.dumps(metrics.to_dict(), indent=2, default=str))
asyncio.run(m())"
```

**Do NOT set `ROOK_ASOF_DATE` for the backtest** — it defaults `season` to
`get_current_season() - 1`, which under as-of resolves to 2024 and finds zero prices.
Pass `2025` explicitly as above, with the clock unset.

### How to read the result

- Base rate for `was_good_buy` is ~50% by construction, so signal accuracy near 50% is noise.
- The earlier open-book run scored 0.904 correlation / 64% signal accuracy. **Ignore it** —
  those projections were built with 2025 in the analysis window.
- An earlier prospective attempt scored 37.3% and is **VOID**: it ran on a board where 15
  of 32 teams had no dependency flags and the market column held 2026 consensus. Both fixed.

---

## Uncommitted work that needs a PR

`scripts/run_predraft_pipeline.py` has uncommitted changes that must be committed:

1. `sync_adp` skips under as-of (live FantasyPros is current-season only)
2. `format_market` skips under as-of (same reason)
3. New `_seed_asof_market()` copies `market_value_historic` into
   `market_value_fantasypros` / `market_value_league` for the as-of season

**Why this matters:** without it the board carried 2026 consensus (Nico Collins $31)
beside the real 2025 auction ($62). Signals compare our value to "market" then get scored
against the 2025 price — two different markets makes the metric meaningless.

Needs: tests (mirror the ones in `tests/unit/utils/test_seasons_asof.py`), then a PR to
`develop`. Do NOT commit `backend/services/trade/trade_demo_source.py` — that's a
pre-existing local edit (`DEMO_CURRENT_WEEK = 3` vs committed `14`) and it is the sole
cause of the 4 permanently-failing tests under `tests/unit/services/trade/`.

---

## Restoring the 2026 board when done

The schema has **no season dimension** — `player_profiles` is `UNIQUE(player_id)`,
`players` valuation columns are single-valued. So the as-of run **destroyed** the 2026
board. It is recoverable only from:

```
backups/rook_2026_board_20260725_0230.sql    (31 MB, 29 tables, 4026 players — verified)
```

**The single-line restore originally written here was WRONG — do not use it.** The dump is
a plain `pg_dump`: 29 `CREATE TABLE`, **0 `DROP`, 0 `TRUNCATE`**. Piped into a populated
database every `CREATE TABLE` fails "already exists" and each `COPY` then **appends** into
the table that still holds the other season's rows — a silently merged two-season board,
with PK violations aborting some copies and leaving the rest partial.

The schema must be reset first. Verified working (no pgvector on this DB — only
`plpgsql`, which lives in `pg_catalog` and survives the drop):

```bash
docker exec rook-dev pg_dump -U postgres -d rook > backups/rook_preRestore.sql
```
```bash
docker exec rook-dev psql -U postgres -d rook -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE;" -c "CREATE SCHEMA public;" -c "GRANT ALL ON SCHEMA public TO postgres;" -c "GRANT ALL ON SCHEMA public TO public;"
```
```bash
docker exec -i rook-dev psql -U postgres -d rook -v ON_ERROR_STOP=1 -q < backups/rook_2026_board_20260725_0230.sql
```

Always dump the current board first (step 1) — it makes the restore reversible, which is
what allows a destructive schema drop to be safe. Always pass `-v ON_ERROR_STOP=1`, or a
failed restore reports success.

Verify afterward that `A.J. Brown` reads `NE` (2026) rather than `PHI` (2025), and that
`players` holds 4026 rows across 29 tables.

The pre-restore dump of the as-of 2025 board is kept at
`backups/rook_asof2025_board_preRestore.sql` (18.8 MB) — it is the only way back to the
board the backtest was run against.

---

## The as-of mechanism (all merged)

`ROOK_ASOF_DATE=YYYY-MM-DD` makes the whole system behave as of that date. Unset = real
time, so forgetting keeps you in the present. Malformed value raises rather than falling
back.

Env var rather than a parameter **because the pipeline shells out** to `seed_nfl_data.py`,
`sync_rosters.py`, `sync_adp.py` via `subprocess.run` — an in-process override is not
inherited.

Guards that fire under as-of, all verified in the last run:

| stage | behaviour |
|---|---|
| `sync_rosters` | SKIPPED (Sleeper is current-state only; seed already wrote as-of teams) |
| `sync_adp` | SKIPPED (uncommitted) |
| `format_market` | SKIPPED (uncommitted) |
| `beat_reporter` | SKIPPED (live RSS has no archive) — known, deliberate gap |
| depth charts | snapshot bounded to the clock; cache key carries the date |
| warehouse rosters | nflverse both sides, deduped to one row per player |
| `seed_nfl_data` | earliest roster week (pre-trade state) |
| beat signal READ | bounded by `flagged_at <= asof_now()` |

Command that produced the current board:

```bash
ROOK_ASOF_DATE=2025-08-15 .venv/Scripts/python.exe scripts/run_predraft_pipeline.py \
  --agent all --full-sweep --skip-seed
```

**`--full-sweep` IS MANDATORY, not optional.** `roster_changes` has a 7-day staleness
skip, and the cache invalidation that would normally clear it lives in `sync_rosters` —
which is correctly SKIPPED under an as-of clock. Without it the stage silently reuses
another season's dependency flags and exits 0. Verified on the 2024 rebuild: 599 flags
still stamped `season_year 2026`, no error raised.

**STAGE THE RUN.** Do seed → `team_systems` → `roster_changes` and VERIFY before
releasing `player_profiles`, which is the dominant cost. On the 2024 rebuild that caught
three silent failures for about $1.10 (see #384) — one of which would have wasted the
entire profiles spend. Every one of them exited 0.

Checks worth running between stages:

```sql
-- flags actually regenerated for the as-of season?
SELECT season_year, count(*) FROM player_dependencies GROUP BY 1;
-- board priced from the RIGHT market? compare against that season's auction
SELECT p.name, p.market_value_fantasypros, h.price
FROM players p JOIN league_auction_history h
  ON h.player_id = p.id AND h.season_year = <season>
ORDER BY p.market_value_fantasypros DESC LIMIT 10;
```

`--skip-seed` is only safe because seed was run separately first, WITH the clock set.
**Skipping seed without having run it as-of leaves 2026 teams on the board** — that
mistake cost $38 once already.

---

## Verified board state (as of the last check)

```
teams          A.J. Brown PHI, Pittman IND, Mecole Hardman GB, Mike Evans TB
mismatch vs the week-1 2025 roster: 0.9% (9 kickers)
dependencies   513 flags, 32/32 teams called, 0 failures
               SEA beneficiaries from DK Metcalf + Tyler Lockett departing
               SF from Deebo Samuel, CLE from Amari Cooper — real 2025 moves
ARI has 0 flags — CORRECT, its departures were all camp bodies below threshold
```

---

## Merged this session (PRs #367–#376)

| PR | what |
|---|---|
| #367 | three valuation double-counts; displayed points = priced points (`players.adjusted_points`) |
| #368 | backtest reads real prices from `market_value_historic`; price-relative scoring |
| #369 | four pipeline correctness bugs + the `ROOK_ASOF_DATE` clock |
| #370 | CLAUDE.md + COST_RULES corrected against actual code |
| #371 | market-value masking (`league or fp` → `max`) |
| #372 | depth charts bounded to the as-of clock |
| #373 | `sync_rosters` refuses under as-of; removed a hardcoded 2026 |
| #374 | warehouse rosters from nflverse under as-of |
| #375 | beat reporter skip + signal date bound |
| #376 | nflverse roster dedupe — fixed a 64x prompt blowup |

---

## Known problems, not yet fixed

**Projection layer is the main error source.** 505 `sonnet_projection` players hold only
132 distinct projected totals. The model does prose arithmetic and rounds ("roughly 7 PPR
per game across ~16 games" → 112.0); 68% of projections ≥20 end in .2 or .8. Buckets: 16
WRs at 52.0, 12 at 18.0. A proposed fix (feed the Python baseline as an anchor, return a
multiplier) was designed and **rejected in verification** — simulation showed MAE vs
market got *worse* in every variant and Jefferson moved $0.03.

**Sonnet routing is over-broad.** 638/749 (85%) of players get a per-player Sonnet call.
A value-at-stake gate was designed and **not shipped**: it lands 15 red tests, and moving
players Sonnet→Haiku flips `profile_source`, which drops them out of
`_PROJECTION_PRICES_FLAGS_SOURCES` and silently **un-gates** the #367 double-count fixes
(measured 98 of 170 players). Also 113 of 170 freed veterans would get `{}` from
`_compute_clean_baseline` (50-touch minimum) — i.e. no projection at all.

**Two routing triggers are dead code.** `career_trajectory` and `contract_year` are never
present in the dict `needs_sonnet_reasoning` receives (`player_entry`,
`player_profiles.py:1652-1673`). They have never fired.

**`_fetch_qb_histories` matches on last-name substring.** "Allen" pairs Josh Allen with
Keenan Allen and Braelon Allen. Same collision class as `_prod_key_full`.

**Beneficiary impact is a flat 35%.** Metcalf's vacated share and Tyler Lockett's are
treated identically. No longer double-counted (#367) but uncalibrated.

**~54 duplicate player-row clusters.** Full dedupe was designed and **rejected**: the
migration read `data/cache/*.parquet` files that are gitignored and absent from the
Railway container, so `alembic upgrade head` in `railway.toml`'s startCommand would crash
the deploy. It also deleted 72 scoreable `value_snapshots` rows and downgraded 4 players'
projections. Deliver as a guarded script under `scripts/`, never a migration.
`Frank Gore Sr` / `Frank Gore Jr` are same-name, same-position, DIFFERENT humans — never
dedupe on a name key.

**`prev_rosters` is not deduped** (only `rosters` is). Harmless — `_handle_departures`
drops duplicates by name — but inconsistent.

---

## Costs, measured from `api_usage_log`

Full sweep is **$9–$72**, not the "<$1.50" `COST_RULES.md` used to claim (now corrected).

`roster_changes` was the dominant cost and is now fixed:

```
              broken(mine)   fixed     pre-regression
avg input      394,188      5,552        ~7,800
$/call          $1.2206     $0.0528      $0.070
per sweep       $27.90      $1.69        $2.24
```

`valuation_agent` dominates **wall clock** (~45 min of a ~75 min run) but is cheap.
Cost and time have opposite shapes.

The dry-run estimator still understates badly — it counts `player_profiles` as 32 team
batches and ignores the ~600 per-player Sonnet calls. `AGENT_SPECS` needs fixing.

---

## Lessons that cost real money

1. **Drive the function, don't reconstruct its inputs from what it wrote.** Three separate
   wrong findings this session came from replaying logic against stored DB state instead
   of actual inputs (Jonathan Taylor "on JAX", `career_trajectory` catching 43%, 96%
   Sonnet routing). All three were wrong.
2. **Stage anything that spends money.** Run one cheap stage, verify, then release the
   rest. This caught a second contamination (trade look-ahead in seed) for $0.
3. **Name-key matching is unsafe everywhere in this codebase.** First-initial+surname
   collides (`jtaylor` → Jonathan Taylor and J'mari Taylor).
4. **Every prescription that skipped adversarial verification was refuted.** The diagnoses
   held; the fixes usually did not.
