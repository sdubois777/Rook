# Build report — real waiver settings replacing the fabricated $100 budget

Written 2026-07-31. Continues `docs/recon/waiver_settings_handoff.md`, which remains
the authoritative description of the problem and the platform field names.

Work is **UNCOMMITTED on `develop`**. Nothing is pushed. Production is unaffected and
still runs `c4f8bb5f`.

---

## 1. What happened

The three jobs listed in section 5 of the handoff are done. An adversarial review of
the finished work then found five more real defects, all now fixed, and five gaps
where a test existed but would not have caught the bug it was written for.

| Job | State |
|---|---|
| 1. The waiver wire page still printed "$100 of $100 budget left" | Done |
| 2. Yahoo's waiver setting was never read into the new fields | Done |
| 3. No tests existed for any of this work | Done — 60 new tests |
| 4. Confirm the demo league still works | Done — it now declares itself a bidding league |

### Defects found while doing the work, and fixed

Five. Each is described by what a customer would have experienced.

**1. A league that does not bid crashed the recommendation after taking payment.**
`backend/services/waiver/faab.py`, the file that turns a lineup improvement into a
suggested dollar bid, called `int()` on the budget. For a league that does not bid
there is no budget, and `int()` of nothing raises. A customer in a Sleeper
rolling-waiver league who pressed "Find waiver targets" was charged 2 credits and then
got a server error. There is no refund path anywhere in the codebase. That function
now accepts "this league does not bid" and returns the same tier label with every
dollar field zeroed.

**2. The recommendation endpoint reported an assumed balance as a fact.** When a
league bids and its budget was read, but the platform never reported how much the
customer's own team had spent, `backend/routers/waiver.py` substituted the whole
league budget as the balance and flagged nothing. The page printed "$200 of $200
budget left" as their real figure — the original defect with a different number. The
other endpoint, which serves the same page, correctly returned "unknown" for the same
team in the same session, so the two contradicted each other. Reachable whenever
ESPN's per-team request fails, and on any Sleeper team that has never made a claim.

**3. The page told customers the wrong thing was unknown.** For a league whose waiver
system could not be read at all, the page said "we could not read your league's own
waiver budget" — which states that only the amount is in question and therefore
asserts the league bids. That is the one claim only the waiver-system field may
authorise. This is not an edge case: the database migration deliberately does no
backfill, so **every existing league is in this state until its next sync**, and
Sleeper leagues have no automatic re-sync path at all.

**4. The page could show another manager's balance as the customer's own money.** The
page picked the acting team as: an explicit choice, else the team the backend
identified as yours, else simply the first team in the list. If a customer's stored
team binding no longer matches any team in the league, no team is identified as theirs
and the first one in the list is a stranger. Their real balance was printed as the
customer's.

**5. Leagues with no waiver process were given a waiver order position.** ESPN reports
a per-team waiver rank on every team of every league whether or not the league uses
waivers — confirmed present on all 176 sampled live leagues, including every
free-agency one. So a league whose own settings say "free agency, no waivers" was
shown "waiver priority #3", contradicting itself in one line. This is the same class
of invented league mechanic as the $100 budget, in a field that is not money.

---

## 2. What each changed file now does

### Files changed in this work

| File | Plain description | What changed |
|---|---|---|
| `frontend/src/lib/waiverSettings.js` | **New.** The rules deciding what the waiver page may say about a league's budget | Decides the budget clause and the waiver system name. The waiver-system check runs first, before any assumption, so an assumed amount can never imply that a league bids. Only real finite numbers are ever printed. |
| `frontend/src/pages/Waiver.jsx` | The waiver wire page | Header states no budget it did not read, and only ever shows a balance for a team that is genuinely the customer's. Once recommendations have run the header describes the same league the bids were priced against. The bid corner shows the tier instead of a dollar amount for leagues that do not bid. A note above the cards names precisely what was assumed. |
| `backend/services/waiver/faab.py` | Turns a lineup improvement into a suggested bid | Accepts "this league does not bid" instead of raising. Returns the same tier and the same worth-claiming decision with every dollar field zeroed and a new `bid_applicable` flag set false. |
| `backend/routers/waiver.py` | Serves the waiver wire page | New `budget_basis` field says what, if anything, was assumed: nothing, the team's spend, the whole budget, or whether the league bids at all. Substituting the league budget for an unknown balance is now recorded as an assumption. Each recommendation carries `bid_applicable`. |
| `backend/integrations/platform_models.py` | Shared data shapes passed between platform integrations and services | Carries waiver system, budget and per-team spend and waiver order. The per-team field is named `budget_spent` because that is what platforms report. New shared constant naming the "no waiver process" system, so two modules cannot drift apart on the spelling. |
| `backend/integrations/sleeper_league_api.py` | Talks to Sleeper's API | Reads the real waiver system and budget; stores spend as spend. |
| `backend/integrations/espn_league_api.py` | Talks to ESPN's API | Reads the real waiver flag and budget; keeps per-team spend and waiver rank it previously discarded. |
| `backend/integrations/yahoo_api.py` | Talks to Yahoo | New `_parse_yahoo_uses_faab`. Reads Yahoo's own flag first, falls back to the waiver rule string, returns "unknown" when neither answers. Reads no budget field at all. |
| `backend/services/league_sync.py` | Imports league data from all three platforms | Stores the waiver settings, including the Yahoo path, which is separate. Yahoo's waiver rule string is capped to the column width so an unexpected value cannot abort the settings sync. |
| `backend/services/trade/real_league_source.py` | Builds the league view the waiver page reads | Balance is budget minus spend. A team whose spend was not reported is left out rather than credited with the full budget. A league with no waiver process gets no waiver order position. |
| `backend/services/waiver/recommendations.py` | Ranks the free agent pool | The budget argument is optional. |
| `backend/services/waiver/waiver_demo_source.py` | The demo league used for screenshots and testing | Declares itself a bidding league, so the demo keeps showing the budget it exists to demonstrate. |
| `backend/models/user_league.py` | Database model for a connected league | Two new nullable columns and a wider waiver type column. |
| `alembic/versions/wvr2026settings_add_waiver_settings.py` | **New.** The database migration | Adds those columns. No backfill. |

---

## 3. What a customer now sees

| Their league | Page header | Recommendation cards |
|---|---|---|
| Bids; budget and their own spend both read | `Week 5, 2026 · Budget · $137 of $200 budget left` | Dollar bids, no caveat |
| Bids; budget read, their spend not reported | `... · Budget · $200 budget, spend unknown` | Dollar bids, above them: "We read your league's budget but not how much you have already spent, so the bids below assume you still have all of it." |
| Bids; budget not readable (all Yahoo bidding leagues) | `... · FAAB · budget unavailable` | Dollar bids, above them: "Your league bids, but we could not read its budget, so the bids below assume $100." |
| Does not bid | `... · Rolling priority · waiver priority #3` | The tier ("league-winner") in place of a dollar amount, plus an explanation that the league claims by priority |
| No waiver process at all | `... · Free agency, no waivers` (no position) | Same as above, without any waiver order claim |
| Waiver system not readable — **every league until it re-syncs** | `Week 5, 2026` and nothing else | Dollar bids, above them: "We could not read how your league handles waivers, so the bids below assume a $100 budget. If your league claims by waiver priority instead of bidding, ignore the dollar amounts and use the ranking." |
| Customer's team could not be identified | No balance shown at all | — |

Yahoo lands in the third row for every bidding league, because no Yahoo field for a
league's budget could be confirmed against a live response. That is deliberate: an
assumption the page admits to is honest; a guessed field name that produces a wrong
number is not. Reading Yahoo's real budget is a worthwhile follow-up.

---

## 4. Tests

74 new tests — 46 backend, 28 frontend. All pass. Every one was checked by breaking
the code it covers and confirming it fails.

| File | Count | Covers |
|---|---|---|
| `tests/unit/integrations/test_waiver_settings_parsing.py` | 16 | Sleeper, ESPN and Yahoo waiver-setting reading, and that "unknown" survives the trip out of the Yahoo settings reader |
| `tests/unit/services/test_waiver_settings_sync.py` | 7 | Storing the setting on the league record, both the shared path and Yahoo's separate one |
| `tests/unit/services/trade/test_waiver_settings_source.py` | 9 | Balance is budget minus spend; unreported spend is not zero spend; no waiver order for a league with no waivers |
| `tests/unit/routers/test_waiver.py` (added to) | 8 | What the two waiver endpoints are allowed to return, including what was assumed |
| `tests/unit/services/waiver/test_waiver_faab.py` (added to) | 6 | The non-bidding branch: same tier, no money, still recommends |
| `frontend/src/test/waiverSettings.test.js` | 16 | The display rules |
| `frontend/src/test/Waiver.test.jsx` | 12 | What the page renders for each league shape |

Every non-bidding test feeds in a budget of 100 on purpose. A test that omitted the
budget from its fixture would pass against the exact bug this work removes.

### Five tests that would not have caught their own bug

The review injected each bug and found the suite still green. All five now fail
against it:

| What was untested | The bug that slipped through | Consequence if shipped |
|---|---|---|
| The bid function's non-bidding branch | Marking every such suggestion "not recommended" | Every add in a rolling-waiver league, including a league-winner, would read "not worth a claim" |
| "Unreported spend" versus "zero spend" | Treating a missing spend as 0 | Every team the platform did not report gets the full budget as a balance |
| The seam between the Yahoo settings reader and the sync | Converting "unknown" to "does not bid" in between | A real Yahoo bidding customer told their league claims by priority, bid withheld |
| The three-state value on the page | Using a truthiness test | "Does not bid" and "we do not know" collapse into one |
| The unknown-waiver-system path | Treating it as a known bidding league | The disclosure this whole feature exists to make, silently dropped |

### Suite state

- Backend `tests/unit`: 2643 collected, 2640 pass, 2 skipped. Collection was 2597
  before these tests, so exactly the 46 new ones and nothing dropped.
- One failure, `tests/unit/services/trade/test_give_side_diversity.py::test_ben_dover_no_longer_ships_allen_in_every_trade`. Pre-existing: it fails
  identically with every file in this work reverted. Not collected by CI.
- Frontend: 393 pass, up from 365 before these tests. ESLint clean on every changed
  file.

---

## 5. The adversarial review

Four agents attacked the finished work from different angles and raised 25 candidate
defects. **The first attempt to verify them failed entirely** — every one of the 14
checking agents died on a session limit, so the run reported "nothing confirmed" when
in fact nothing had been checked. The verification was re-run.

Of the deduplicated claims: five were confirmed and are fixed (section 1); eight were
refuted as either not defects or not reachable from any real platform response or
database state. Two reviewing agents modified files in the working tree during the
review despite being told not to; the tree was checked afterwards and is clean.

---

## 6. The database migration

`alembic/versions/wvr2026settings_add_waiver_settings.py`. Development is already at
this revision. Production is at `src2026tenant` and has not seen it.

Checked, without touching any database:

- It is the single head. All 59 migration files were parsed and the chain
  reconstructed; there is one base, no duplicate revision ids, and no other file
  anywhere in the repository — across all 65 git refs — claims `src2026tenant` as its
  parent. `alembic heads` agrees.
- The columns match `backend/models/user_league.py` exactly.
- The generated SQL is three statements inside one transaction: two column additions
  and one type widening. In PostgreSQL a length increase is a catalogue change, so
  there is no table rewrite and no long lock. This matters because `railway.toml`
  starts the service with `alembic upgrade head && uvicorn`, so a slow or failing
  migration is downtime rather than a failed deploy.

The downgrade has **not** been rehearsed against a live database, because doing so is
a destructive operation and needs your say-so. Its generated SQL was inspected and is
correct.

---

## 7. What is left

- **You approve and merge.** Nothing is committed. The release path is a pull request
  into `develop`, then a separate reconcile branch into `main`.
- **`.gitignore` must stay out of the commit.** It is modified in the working tree
  (it adds `.gstack/`) and is not part of this work.
- **Worth doing next, not done:** read Yahoo's real league budget, so Yahoo bidding
  leagues stop being sized against an assumed $100.
- Section 7 of the handoff lists two problems still not started: the weekly matchup
  page inventing the opposing team, and ESPN and Sleeper rosters carrying no injury
  status or lineup position. Both affect features that charge credits.
