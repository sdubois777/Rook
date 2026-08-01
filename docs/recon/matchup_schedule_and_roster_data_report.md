# Build report — the real weekly opponent, and roster injury and lineup data

Written 2026-08-01. These are the two problems listed as not started in section 7 of
`docs/recon/waiver_settings_handoff.md`.

---

## 1. What was wrong

### The weekly matchup page invented the opposing team

`backend/routers/matchup.py`, which serves the weekly matchup page, called a
round-robin pairing generator unconditionally — for every league, on every week — and
nothing on the page said the result was made up. All three platform readers had a
method for fetching the real schedule and all three returned an empty list; the method
was never called from anywhere.

Measured against three real leagues over three weeks (102 team-weeks), the invented
pairing named the correct opponent **10 times**.

Everything the page says about "your opponent" is computed against that team: the
projected points margin, the win-likelihood band, the positional comparison grid, and
the trade opening it hands off to the trade analyzer, which charges 1 credit. So a
customer was shown a scouting report on a team they were not playing, and could act on
it for money.

The generator also has two defects of its own. Its pairings repeat every 11 weeks in a
12-team league, so week 12 is identical to week 1. And in a league with an odd number
of teams, the same manager is given a bye every single week, all season.

### Rostered players carried no injury status and no lineup position

`backend/services/trade/real_league_source.py`, which turns synced platform rosters
into the shape the value engine reads, hardcoded the lineup slot to nothing and read an
injury field that no platform reader ever filled. So on every real league, on **all
three platforms**, both were absent for 100% of rostered players.

That silently disabled every filter that keeps an unavailable player out of a lineup.
The most expensive consequence is on the waiver recommendation, which charges 2 credits
before doing any work and has no refund path: the baseline lineup that a new player has
to beat included the full weekly points of a player who cannot play, so the addition
that would actually fill the hole scored near zero improvement, and the customer was
told nothing on waivers cracks their starting lineup.

Rosters also rendered as entirely bench, because nothing ever said who was starting.

---

## 2. What each changed file does now

| File | What it is | What changed |
|---|---|---|
| `backend/integrations/platform_api.py` | The interface all three platform readers implement | The schedule method can now report "we could not tell" as distinct from "this league genuinely has no game this week". Returning an empty list for both is why the page could never tell when to stop inventing |
| `backend/integrations/platform_models.py` | The shared data shapes passed between platform readers and services | The lineup field was a plain true/false, where false claimed a player is benched. It is now a named slot that can be genuinely unknown |
| `backend/integrations/sleeper_league_api.py` | Reads league data from Sleeper | Reads the real weekly schedule, and each player's lineup slot and injured-reserve placement |
| `backend/integrations/espn_league_api.py` | Reads league data from ESPN | Reads the real weekly schedule, and stops discarding each player's lineup slot and injury value |
| `backend/integrations/yahoo_league_api.py` | Reads league data from Yahoo | Reports its schedule as unknown, and leaves lineup slot and injury unset. Nothing about Yahoo could be verified |
| `backend/utils/injury_status.py` | Converts each platform's injury wording into the codes the app uses | Recognises ESPN's wording for injured reserve |
| `backend/services/trade/real_league_source.py` | Turns synced rosters into what the value engine reads | Fills in both fields, choosing the strongest evidence available |
| `backend/routers/matchup.py` | Serves the weekly matchup page | Fetches the league's real schedule and shows no opponent at all when there is none to read |
| `frontend/src/lib/lineupSlots.js` | **New.** Decides what counts as a starting slot | Injured reserve is not a starting slot |
| `frontend/src/pages/Trade.jsx` | The trade page | Uses that, so a player on injured reserve is no longer shown among the starters |
| `frontend/src/pages/Matchup.jsx` | The weekly matchup page | Says when the schedule could not be read, instead of blaming a bye week |

---

## 3. How the injury evidence is chosen

In order, strongest first:

1. **The manager's own injured-reserve placement.** That is the customer's actual
   league state, and no league-wide injury feed can express it.
2. **A designation the platform sent with the roster**, converted from that platform's
   own wording. An unrecognised spelling produces no badge, with a warning.
3. **The stored player record**, but only if recent enough to stand behind.
4. **Nothing.** No badge, and the lineup math treats the player as available.

The stored record refreshes on a weekly sweep, so a value about a week old is normal
operation. The ceiling of 10 days exists to catch that sweep having stopped: past it,
nothing is shown rather than something months old.

---

## 4. What was verified, and how

| Claim | How |
|---|---|
| ESPN publishes the real schedule before games are played | The operator's own ESPN league. All 84 entries of the 2026 season — 14 weeks by 6 games — returned with no games played |
| ESPN's injury wording | The operator's own league's player list. Across 1,026 players: `ACTIVE`, `QUESTIONABLE`, `OUT`, `INJURY_RESERVE` |
| ESPN lineup slot numbers | Already confirmed in the repository against a real settings sample, pinned by an existing test. Reused rather than duplicated |
| Sleeper publishes the real schedule for unplayed weeks | Public API. Available once a league's draft is complete |
| Sleeper lineup slots align by position in the starters list | Public API, verified across a large sample. Must come from the roster endpoint; the per-week matchup snapshot misaligns in about 0.5% of leagues |
| Reading injury from our own database instead would be enough | **Refuted.** Measured against live data, 38% of the badges it would show are wrong, it refreshes only weekly, and it cannot express a manager's injured-reserve placement |

**A defect found while verifying.** ESPN's wording for injured reserve was not
recognised by the converter, so those players produced no badge and were treated as
healthy — the identical symptom to reading no injury data at all, while appearing to
work. It is now mapped and pinned by a test.

---

## 5. Yahoo gets no guesses

Yahoo documents a schedule resource, a per-player status, and a starting-slot field.
None could be checked: its API refuses unauthenticated requests and there is no
recorded sample anywhere in this repository. All three therefore report unknown.

Two tests fail if someone later fills them in from documentation alone. A wrong lineup
slot renders as a confident, valid-looking starting position, and a missed injury
silently seats a player who cannot play.

---

## 6. Tests

Every new test was checked by reverting the code it covers and confirming it fails.
Several were checked against more than one wrong implementation, because a single
revert does not always exercise the behaviour a test names.

| Area | Checked against |
|---|---|
| Sleeper schedule | The old empty-list stub; and separately, making an empty week report as unknown |
| Sleeper lineup slots | Never setting them; labelling unknown shapes as bench; defaulting to bench when the slot list is unreadable |
| ESPN schedule and roster fields | The old stub; and removing the two discarded fields |
| Injury wording | Removing the injured-reserve mapping |
| Injury precedence | Restoring the original hardcoded line; removing the injured-reserve precedence; removing the freshness check |
| Matchup page endpoint | Inventing a pairing whenever the real one is missing |
| Matchup page display | Collapsing all no-opponent reasons into one message |
| Yahoo | Reading its documented field names anyway |

**One test I wrote did not exercise the bug it named**, and I rewrote it to target the
real risk. **One defect in my own code was caught by a test I wrote**: the Sleeper
lineup reader labelled every player "BENCH" on a roster shape it did not recognise,
which is a positive claim about every player. It now leaves those unknown.

### Suite state

- Backend: 2680 passing, up from 2640 before this work.
- Frontend: 402 passing, up from 393.
- One backend failure, `tests/unit/services/trade/test_give_side_diversity.py::test_ben_dover_no_longer_ships_allen_in_every_trade`. Pre-existing: it fails
  identically with all of this reverted, and is not collected by CI.

---

## 7. Known limits

- **ESPN's lineup slots and injury values have never run against a real drafted ESPN
  roster.** The test league available could not draft without more managers, so those
  two readers are unit-tested but not exercised end to end on live roster data.
- **The round-robin pairing generator is still used for the demo league**, and still
  has the two defects described in section 1. Nothing real depends on it now.
- **Yahoo remains unimplemented for all three fields**, by choice.
- **No database migration.** Nothing here changes the schema.
