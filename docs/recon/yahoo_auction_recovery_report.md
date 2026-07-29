# Yahoo auction row recovery — the repair existed; it was not safe to run

Task from `docs/recon/replacement_floor_handoff.md` §5, first bullet: *"the Yahoo league
sync writes unscoreable auction rows … 152 of the 180 resolve via
`split_part(yahoo_player_key,'.',3)` → `players.yahoo_id`, so it is recoverable. This is
the highest-value open item: without it prod can never score its own league."*

Branch `fix/auction-identity-backfill-prod-safety`, cut from `develop` at `b4c8388`.

---

## 1. What was already done, and what actually remained

Both halves of the obvious fix were already in the tree:

- **The writer is fixed.** `LeagueSyncService._resolve_pick_identities`
  (`backend/services/league_sync.py:359`) maps `yahoo_player_key` → `players.yahoo_id` for
  the whole draft in one query, drops ambiguous keys, and `_store_picks` reports a
  `resolved` count. New syncs are fine.
- **A backfill script exists** for rows already stored: `scripts/backfill_auction_identity.py`.

So the recovery logic was not missing. What was missing was any reason to trust the script,
and one specific defect made running it actively misleading.

## 2. The defect: the documented prod command silently targeted dev

The script's usage block read:

```
PROD_DATABASE_URL=... ROOK_ALLOW_PROD_WRITES=1 \
    .venv/Scripts/python.exe scripts/backfill_auction_identity.py
```

`PROD_DATABASE_URL` is a real convention in this repo — `prod_snapshot.py`,
`migrate_validate.py`, `refresh_dev_db.py` and others read it with
`os.environ.get("PROD_DATABASE_URL")` and build their own engine. **This script does not.**
It connects through `backend.database.AsyncSessionLocal`, i.e. `settings.database_url`,
which is selected by `ROOK_ENV_FILE` (`backend/config.py`). Setting `PROD_DATABASE_URL`
changes nothing.

The failure mode is worse than "the command doesn't work":

1. The run connects to **dev** (the default `.env`).
2. `guard_writes` sees a dev host and stays silent — the `ROOK_ALLOW_PROD_WRITES=1` in the
   command is irrelevant, so nothing signals the mistake.
3. Dev has no id-less rows, so the script prints **"Nothing to backfill — every row already
   has a player_id."**
4. That reads as a clean bill of health *for prod* — a database the process never opened.

A prod repair whose documented invocation produces a false all-clear is worse than one
that errors. Fixed: the usage block now names `ROOK_ENV_FILE=.env.prod`, and **every run
prints the host and prod-flag it is about to act on** before doing anything:

```
backfill_auction_identity: db host=localhost  is_prod=False  mode=dry-run
```

## 3. Three further gaps closed

**a. "Resolved: N" did not answer the question.** The backtest keys on
`price > 0 AND player_id IS NOT NULL` and needs `MIN_PRICE_COVERAGE` (50) distinct players
before it will use league prices at all. A row that resolves perfectly but carries
`price = 0` buys nothing. The script now reports the count that actually decides it, per
season, against the real threshold imported from `backtest` rather than restated:

```
Priceable players per season (backtest needs >= 50):
  2022: 174            USES LEAGUE PRICES
  2099: 0 -> 54        USES LEAGUE PRICES
  2023: 0 -> 0         falls through to ADP
```

**b. One constraint violation would have rolled back the whole repair.**
`uq_auction_player_season_source` is on `(player_id, season_year, source)`. Rows with
`player_id IS NULL` never conflict in Postgres, so duplicates coexist today and collide
only at the instant identity is written — mid-commit, on a one-shot prod script, losing
every other row's repair with it. Claimed keys are now tracked and colliding rows skipped,
consistent with the existing "ambiguous keys are skipped, never guessed" rule.

**c. Zero tests.** `plan_backfill` and `build_lookup` are now pure functions with the I/O
lifted out, and `tests/unit/scripts/test_backfill_auction_identity.py` covers 17 cases:
the happy path, ambiguous yahoo_ids skipped rather than guessed, unmatched ids, malformed
keys, collisions against existing and against sibling rows, planner replayability,
price-0 rows resolving but not counting as priceable, and the report's threshold verdict.
One test asserts the docstring cannot re-acquire the `PROD_DATABASE_URL` trap.

## 4. A note on the handoff's suggested SQL

The handoff proposes `split_part(yahoo_player_key,'.',3)`. That is correct for Yahoo's real
`player_key`, which is three parts (`461.p.33963`) — but it is positional, and returns the
wrong field for anything longer. The script uses `rsplit(".p.", 1)[-1]`, which is right for
both shapes. The rehearsal below deliberately seeds one over-long key to prove it, and one
malformed key to prove no fallback splits on a bare `.`. Worth knowing before anyone runs
the `split_part` form directly against prod.

## 5. Verification

**Unit:** 17 new tests pass. Full suite 2501 passed, 2 skipped, 1 failed — the
pre-existing `test_give_side_diversity.py::test_ben_dover_no_longer_ships_allen_in_every_trade`
the handoff documents as expected on clean `develop`.

**End-to-end against real Postgres.** The pure planner being tested is not enough: what
bites on prod is the database — the unique constraints, NULL-doesn't-conflict, and whether
the UPDATE commits. So 64 rows in exactly the shape the Yahoo sync wrote
(`yahoo_player_key` only, no `player_id`, empty `player_name`/`position`) were seeded on dev
under throwaway season 2099, the real script was run, the outcome checked, and the rows
removed. Dev is byte-identical to its baseline afterwards (2022/2023/2024 at
174/175/177 rows, all with ids, names and prices).

| | |
|---|---|
| seeded | 64 |
| resolved | 60 (54 priced, **$1674 bound**) |
| ambiguous → skipped | 1 (a yahoo_id matching two player rows) |
| collision → skipped | 1 (same player, season and source) |
| unmatched | 1 |
| malformed key | 1 |
| **rows bound to a player whose yahoo_id ≠ the key** | **0** |

Season 2099 went `0 → 54` priceable, crossing the threshold to `USES LEAGUE PRICES`. No
`IntegrityError`: the collision was skipped, not committed.

## 6. Not done, and why

- **Prod was neither read nor written.** The permission layer in this session blocks
  `ROOK_ENV_FILE=.env.prod` even for read-only queries, so the 180 rows / $2367 / "152
  resolve" figures remain the handoff's, unverified by me. **The repair is ready but has
  not been run.** The commands are in the script's usage block; dry-run first, it needs no
  override. Expect the report to name the host — check it says a `rlwy.net` host and
  `is_prod=True` before trusting the numbers.
- **Prod's 2023 season will not be fixed by this.** The handoff records it as 160 rows with
  *no prices*. Identity backfill cannot help: `price = pick.auction_price or 0`
  (`league_sync.py:452`) and Yahoo's adapter sets `auction_price` only when the pick carries
  a `cost` field (`yahoo_league_api.py:254`), which it does **not** for snake drafts. If
  2023 was a snake year there is no auction to recover, ever, and the correct outcome is for
  the backtest to refuse that season rather than score it. The script now says so explicitly
  when it sees resolved-but-unpriced rows.
- **The remaining fall-through is a measurement-integrity issue, not a data one.** When a
  season cannot be priced, `backtest.py:972` uses `player.market_value_league` — current
  ADP — and computes every signal metric against it. It *is* labelled in `price_source`,
  and `_warn_unusable_auction_rows` logs loudly, but nothing refuses. Worth deciding
  whether a season with no real prices should score at all; out of scope here, and it
  should not be conflated with the recovery.
