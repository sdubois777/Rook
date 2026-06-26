# Rook — ESPN Resolver Design (locked from v2 recon)

Host / match pattern: `https://fantasy.espn.com/football/draft*`
Framework: React + styled-jsx (Next.js). No stable root id. **Gate on content, never on root.**
Anchor policy: `data-testid` and hand-authored semantic classes = PRIMARY. `jsx-<digits>` = FALLBACK ONLY (styled-jsx, rotates per deploy) — same discipline as Yahoo `_ys_*`: behind a text/structure check, with `console.warn` + `selector_health='fallback'`. Strip `data-darkreader-*` in the sanitizer (DarkReader extension noise, not ESPN).

---

## Format gate (content-based, testid-driven)

| Format | Active when | Inert when |
|---|---|---|
| Salary cap (→ auction events) | `[data-testid="auction-pick"]` present (≈12) **and/or** `[data-testid="bidding-form"]` present | no `auction-pick` |
| Snake (→ snake events) | `[data-testid="current-pick"]` present **and** board cells carry `.roundPick` **and** no `[data-testid="auction-pick"]` | `auction-pick` present |

Cross-poller: zero Yahoo `ys-*` hooks on ESPN; host split + testid gate keeps each poller inert on the other platform.

---

## Shared spine: clock + pick counter

`[data-testid="clock"]` → `.clock__label` + `.clock__digits`.
- Salary cap label: `PK {n} OF {total}` (e.g. `PK 1 OF 192`). Between picks digits show `--:--`.
- Snake label: `RND {r} of {R}` (e.g. `RND 13 of 16`).
- Snake also: `[data-testid="current-pick"]` → `.on-the-clock` ("On the Clock: Pick 156") + `.team-name` ("Team 12"), `title` = on-clock team.

---

## Salary-cap resolver → `nomination / bid_update / clock / draft_pick / teams_update`

Source anchors:
- **Pick train** `[data-testid="draft-order-set"]` → 12 × `[data-testid="auction-pick"]`. Each: `title`=team name, `.team-name` ("2. Stephen's Smart Team" — strip leading "N. "), `.cash`=budget remaining, `.bid-amount`=current/last bid, `[data-testid="auto-pick"]`/`.autopick`=autopick on. Content modifiers: `auction-pick-component--selecting` = team currently nominating; `auction-pick-component--own` = **my team**.
- **Nominee on the block** `[data-testid="player-selected"]` → full name + pro team + position (+ stats text).
- **Bidding** `[data-testid="bidding-form"]` → "Current offer: $52" (current high bid) + "Manual offer (max $185)" (my remaining biddable).
- **Sale** `[data-testid="player-drafted"]` → name, "Team / POS", `.player-drafted__price` ($70); `player-drafted--own` = my win.

Event mapping:
- `clock` ← clock digits + `PK n OF total`.
- `nomination` ← new `player-selected`; nominating team = `--selecting` auction-pick.
- `bid_update` ← `bidding-form` current offer; high bidder via the team whose `.bid-amount` equals current offer (confirm marking live — see open items).
- `draft_pick` (sale) ← `player-drafted` banner: player + price; winner via **budget-delta** across `auction-pick .cash` (Yahoo #117 pattern); `is_yours` via `player-drafted--own`.
- `teams_update` ← iterate 12 `auction-pick`: name, budget, bid, own-flag, autopick.

Self-team: `auction-pick-component--own` (primary) + URL `teamId` (cross-check).

---

## Snake resolver → `your_turn / your_turn_soon / snake_status / snake_pick`

Source anchors:
- **Status** `clock` (`RND r of R` + digits) + `current-pick` (pick number + on-clock team).
- **Pick train** `.picklist` → upcoming `.pick-component`: `.pick-number` ("PICK 157"), `.team-name`, `.auto-word`. Round dividers `.picklist--divider`.
- **Board (full history, non-destructive)** `.draft-board-grid-pick-cell.completedPick` → `.roundPick` ("1.1" = round.pickInRound), `.playerFirstName` + `.playerLastName`, `.playerProTeam`, `.positionPill`, `.byeWeek`. Upcoming cells: `.upcomingPick` + `.roundPick`. Cell position via inline `grid-area: {row} / {col}`.
- **Self pick / turn banner** `[data-testid="player-drafted"]` ("You drafted Ja'Marr Chase!", `player-drafted--own`); "You are on the clock!" banner = your_turn; "You're on Autopick" banner = autopick state.

Event mapping:
- `snake_status` ← each poll: round, current pick number, on-clock team, seconds (additive emission, PR #104 pattern).
- `your_turn` ← "You are on the clock!" banner OR `current-pick` team == my team.
- `your_turn_soon` ← count picks in `.picklist` until my next `.pick-component` → picks_until.
- `snake_pick` ← new `completedPick` (track by global pick from `PK n` / `roundPick`); player from name spans + team/pos; **drafting team = board column header** (see open items); `is_yours` via `player-drafted--own`.

Self-team: "You are on the clock!" / `player-drafted--own` + URL `teamId`; board column via own-marker/custom-name match (confirm — see open items).

---

## Player resolution (id-first, name-backstop)

Both resolvers emit on every pick/nomination: `espn_player_id` (if obtainable), `name`, `pro_team`, `position`. Backend order:
1. `espn_player_id` → canonical UUID map (if id present).
2. Else **name + pro_team + position** normalization (reuse Yahoo path). ESPN gives FULL names + team + pos → reliable key.
3. Else `unresolved`.

Emit `resolution_source` = `id_map | name_backstop | unresolved` on the enriched event (mirrors `selector_health`). `name_backstop` → log; `unresolved` → loud warn. Guards against #117-style silent drop, per-pick observable.

**ESPN reality:** pick/sale/board surfaces are name-only (`data-player-id` lives in the player-pool table, not on picks). So name_backstop is the common path for picks; full name+team+pos makes it dependable. `espn_id` backfill is therefore opportunistic, not blocking — decide whether to populate now or defer.

---

## Open items before resolver code (arrive in full mid-draft fixtures)

1. **Board column → team mapping** (both formats): capture the board header row (`.header-cell` / `.team-logo`, ≈26–54 nodes) to map grid column → team, and the self-column marker. Required for `snake_pick` and full board state.
2. **Salary-cap `completedPick` board-cell anatomy**: only upcoming `rosterSlot` cells captured so far; need a mid-draft salary-cap board to confirm completed-cell structure (player + price under team column). Snake `completedPick` already confirmed.
3. **Live high-bidder marking** during a multi-bid auction moment; and whether `[data-testid="player-selected"]` carries `data-player-id` (would enable id-first on the nominee). Quick check:
   `['player-selected','bidding-form'].forEach(t => console.log(t, document.querySelector('[data-testid="'+t+'"]')?.outerHTML))`

---

## Ship plan

Branch off `develop`, stop at the develop merge. Likely shape: `espn_salarycap_resolve.mjs`, `espn_snake_resolve.mjs`, content-based ESPN gate, fixtures under `extension/test/fixtures/espn/` (lobby / on-the-clock / nomination+bid / sale / board-mid-draft / complete), cross-poller assertions (ESPN pollers inert on Yahoo fixtures and vice-versa, same pass). Preserve the event contract so backend/frontend stay untouched; verify both ends consume the fields before claiming downstream-untouched. Account for any test-count delta explicitly.
