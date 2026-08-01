# Handoff — real waiver settings instead of a fabricated $100 budget

Written 2026-07-31. Work is IN PROGRESS and UNCOMMITTED on `develop`.

---

## 1. Read this first: the state of the working tree

Branch `develop`. Nothing committed. Nothing pushed. Production is unaffected.

**Modified, tracked:**

| File | What it is | What the change does |
|---|---|---|
| `backend/integrations/platform_models.py` | Shared data shapes passed between platform integrations and services | Adds waiver system, budget, per-team spend and waiver order position. Renames the per-team field `faab_remaining` to `budget_spent`. |
| `backend/integrations/sleeper_league_api.py` | Talks to Sleeper's API | Reads the real waiver system and budget; stops storing spend in a field meaning remaining. |
| `backend/integrations/espn_league_api.py` | Talks to ESPN's API | Reads the real waiver flag and budget; keeps per-team spend and waiver rank it previously discarded. |
| `backend/models/user_league.py` | Database model for a connected league | Adds `uses_bidding_budget` (nullable boolean) and `waiver_budget` (nullable integer); widens `waiver_type` to 30 characters. |
| `backend/services/league_sync.py` | Imports league data from all three platforms | Stores the waiver settings onto the league record. |
| `backend/services/trade/real_league_source.py` | Builds the league view the trade and waiver features read | Replaces the hardcoded `"faab"` label and `100` budget with the league's real values; computes remaining as budget minus spend; carries the league record and raw rosters so those values are reachable. |
| `backend/routers/waiver.py` | Serves the waiver wire page | Response can now express "unknown", and carries a flag saying whether a figure is the league's own or a stated assumption. |
| `tests/unit/integrations/test_platform_models.py` | Tests the shared data shapes | Updated for the renamed field. |

**New, untracked:**
- `alembic/versions/wvr2026settings_add_waiver_settings.py` — the database migration.

**Also modified but NOT MINE — leave it out of any commit:** `.gitignore`. It was already modified before this work started.

**Local database state:** the development database is at migration `wvr2026settings`. Production is at `src2026tenant` and has NOT seen this migration.

Full test suite currently: 2622 passing, 2 skipped. One failure, `tests/unit/services/trade/test_give_side_diversity.py::test_ben_dover_no_longer_ships_allen_in_every_trade`, is pre-existing, unrelated to this work, and not collected by CI.

---

## 2. What problem this solves

The waiver wire recommendation charges 2 credits, takes the charge BEFORE doing any
work (`backend/services/credit_service.py:90`), and has no refund path anywhere.

It then computed a dollar bid against a budget of 100 that was never read from the
customer's league, and `frontend/src/pages/Waiver.jsx` line 305 displayed
`"$100 of $100 budget left"` for every customer regardless of their real league.

Leagues that use rolling waiver priority — which do not bid at all — were labelled
as budget leagues and given a dollar figure anyway.

The original code recorded this as a known shortcut. `backend/services/waiver/faab.py`
line 24 says: *"Demo league budget. Real leagues will read this from the league
settings once waiver settings are persisted (explicit follow-up — not built in v1)."*

---

## 3. The trap that shapes the whole design

**Every platform reports a budget value even for leagues that never bid.**

Measured against live APIs, not documentation:

- Sleeper: a `waiver_budget` value was present on **285 of 285** live leagues,
  including every rolling-waiver league, almost always as `100`.
- ESPN: **34 of 35** live non-bidding leagues carried an `acquisitionBudget` of `100`.

So reading the budget field without first checking the waiver system reproduces the
exact fabricated `$100` figure, just sourced differently. The waiver SYSTEM field is
the only thing that may decide whether a dollar figure means anything.

### Confirmed field names

| Platform | Field deciding whether the league bids | Budget field | Per-team field | Evidence |
|---|---|---|---|---|
| Sleeper | `settings.waiver_type` (integer; `2` means bidding, `0` and `1` do not) | `settings.waiver_budget` | `rosters[].settings.waiver_budget_used` = amount SPENT; `waiver_position` for priority leagues | 285 live leagues |
| ESPN | `settings.acquisitionSettings.isUsingAcquisitionBudget` (boolean) | `acquisitionSettings.acquisitionBudget` | `teams[].transactionCounter.acquisitionBudgetSpent` = amount SPENT; `teams[].waiverRank` for priority | 57 live leagues |
| Yahoo | `uses_faab` (string `"0"` or `"1"`) | not yet established | not yet established | **DOCUMENTATION ONLY — NOT VERIFIED LIVE**, because Yahoo's Fantasy API is behind their approval process |

**ESPN second trap:** `acquisitionType` does NOT indicate bidding. The value
`WAIVERS_TRADITIONAL` appears with the bidding flag both true (17 leagues) and false
(34 leagues). It describes when claims process, not whether they are bid on. Also
note the value is `WAIVERS_CONTINUOUS`, not `WAIVERS_CONTINUAL`.

**No extra network calls were needed.** Both platforms already fetch the responses
containing these fields and were discarding them.

---

## 4. The three cases the product must handle

The operator's direction: not all waiver wires use budgets, but a budget league is
generally $100.

| League situation | What the customer should get |
|---|---|
| Bids, budget readable | Real budget, real remaining balance, dollar bid |
| Bids, budget NOT readable | Dollar bid based on a $100 budget, **labelled on the page as an assumption**, not presented as their league's real figure |
| Does not bid | No dollar figure and no budget claim. The ranking and descriptive tier remain, and the page names the league's actual waiver system |

The third case works because the useful part of the recommendation does not depend on
a budget. `backend/services/waiver/faab.py` derives the tier labels
("league-winner", "week-winning starter", "flex / matchup play", "speculative stash")
from projected lineup improvement. Only the dollar amount is budget-dependent.

`uses_bidding_budget` is deliberately three-state everywhere it appears:
`None` means unknown and nothing may be claimed; `True` means the league bids;
`False` means it does not. Note `backend/services/league_sync.py` uses
`is not None` rather than a truthiness test when storing it, because `False` is a
real answer that a truthiness test would discard.

---

## 5. What remains to be done

1. **`frontend/src/pages/Waiver.jsx`** — the page still prints
   `"$100 of $100 budget left"` at line 305, and line 293 reads
   `myTeam?.faab_remaining ?? league.faab_budget`. Both need to handle the three
   cases above, including surfacing the new `budget_is_assumed` flag from the
   response. This is the last place the fabricated figure is visible to a customer.

2. **Yahoo** — `backend/integrations/yahoo_api.py` `get_league_settings` already
   parses the settings response and already computes a `uses_faab` value. Wire it
   into the same fields. Treat the field names as unverified and fail to "unknown"
   rather than guessing.

3. **Tests.** None have been written for this work yet. Every test must be checked
   by reverting the code change and confirming the test fails. Cover at minimum:
   a Sleeper league with `waiver_type` 0 must NOT report a budget despite carrying a
   `waiver_budget` of 100; an ESPN league with `isUsingAcquisitionBudget` false must
   do the same; remaining must equal budget minus spend, not spend; and a league with
   unknown settings must produce no budget claim.

4. **Check the demo path still works.** `backend/services/waiver/waiver_demo_source.py`
   has its own `faab_remaining_by_team` and was not changed. Confirm the router's new
   `getattr(src, "uses_bidding_budget", None)` reads sensibly for it.

---

## 6. Working practices this project uses

- **Deep research before writing code, and adversarial verification of findings.**
  Several findings in this work were overturned by checking them; do not accept a
  claim without evidence.
- **Every test is verified by reverting the code change and confirming it fails.**
  A test that passes both ways proves nothing. This repeatedly caught weak tests.
- **Anything found broken along the way gets fixed, not logged.** The operator has
  said explicitly they do not want a growing list of tracked issues.
- **Migrations run when the service starts** — `railway.toml` uses
  `alembic upgrade head && uvicorn`. A failing migration stops the service rather
  than failing a deploy. Rehearse every migration on the development database, and
  check preconditions read-only against production before releasing.
- **Release path:** pull request into `develop`, then a separate reconcile branch
  into `main`. See `CLAUDE.md`. The operator approves each merge and release.
- **Selecting the production database is per-script.** `ROOK_ENV_FILE=.env.prod`
  selects production for scripts that read `backend/config.py`. `PROD_DATABASE_URL`
  is NOT interchangeable and using the wrong one silently runs against development.
  Always print the resolved host before doing anything.

---

## 7. Other known problems, not started

From an audit of the ESPN and Sleeper customer journeys. Both affect features that
charge credits.

- **The weekly matchup page invents the opposing team.**
  `backend/routers/matchup.py` line 217 calls a round-robin generator with no check
  for demo mode, and all three platform implementations of the real-schedule method
  return an empty list. Nothing marks the opponent as invented. The page then offers
  "Explore a trade with [invented team]", leading into the 5-credit trade finder.
  Real implementations exist: ESPN's `mMatchup` view and Sleeper's
  `/league/{id}/matchups/{week}`. Both are reachable with the existing helpers.

- **ESPN and Sleeper rostered players carry no injury status and no lineup position.**
  `backend/services/trade/real_league_source.py` hardcodes the lineup position to
  none. ESPN's roster response contains both `lineupSlotId` and `injuryStatus` and
  they are being discarded. This feeds the trade analyzer (1 credit) and trade
  finder (5 credits) with empty injury data.
