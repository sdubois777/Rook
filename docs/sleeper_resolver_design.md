# Rook — Sleeper Resolver Design (locked from live WS capture)

Host / match: `https://sleeper.com/draft/*` + `https://sleeper.app/draft/*`
Transport: **Phoenix Channels over WebSocket** (Elixir/Phoenix). NOT DOM. The whole
draft is clean JSON on the `draft:<draft_id>` topic — the most robust target of the
three platforms (nothing to break on a UI redeploy; only the Phoenix event names
could change).

## Why a `world:"MAIN"` interceptor
Sleeper's CSP blocks page **inline** scripts, so the shared `injectInterceptor()`
(inline `<script>` injection) silently fails — `window.__rook_intercepting__` stays
undefined and `window.WebSocket` is never patched. Fix: register the WebSocket patch
as a **`world:"MAIN"` content script** at `document_start` (`sleeper_draft_main.js`),
which the browser injects (CSP doesn't apply). It re-broadcasts each received frame
to the ISOLATED poller via **`window.postMessage`** (CustomEvent `detail` does not
cross worlds in Chrome; frame data is a JSON string → structured-cloneable).

## Phoenix frame format
`[join_ref, ref, topic, event, payload]`. Act ONLY on `topic` matching
`/^draft:(\d+)$/` — ignore `presence_draft:*`, `phoenix` heartbeats, and non-arrays.

## Self-team
My Sleeper `user_id` from the page's `localStorage.getItem('user_id')` (content
scripts share the page's localStorage). `draft_updated_*.draft_order` maps
`{user_id: slot}` → my slot → drives `your_turn` / `is_yours`. The socket URL carries
no token, so localStorage is the anchor.

## Player resolution — id-first (the clean case)
Every pick/sale frame's `player_id` is a **Sleeper id**, matched exactly against
Rook's indexed `players.sleeper_id` (`resolution_source = id_map`). Snake/auction
**sale** frames also carry full `metadata` (name/team/position) → name backstop works
too. **Auction nomination/bid frames are player-id-only (no name)** — so the backend
gained an additive `_resolve_player(..., sleeper_id=...)` → `find_by_sleeper_id` tried
first (Yahoo/ESPN send no sleeper_id → unaffected). The nomination broadcast is
backfilled with the resolved name so the UI shows the nominee.

---

## Snake — `sleeper_snake_resolve.mjs` → snake_pick / snake_status / your_turn / your_turn_soon

| Sleeper event | → contract |
|---|---|
| `player_picked` (`player_id`, `pick_no`, `metadata{first_name,last_name,position,team}`) | **`snake_pick`** — `round=ceil(pick_no/teams)`, `picker`=serpentine slot, `is_yours`=slot==my slot |
| `draft_updated_by_pick` (`type`, `status`, `settings{teams,reversal_round}`, `draft_order`) | config; derive **`snake_status`/`your_turn`/`your_turn_soon`** from pick stream + my slot |

Serpentine: round 1 forward, even rounds reverse; `reversal_round>0` (Nth-round
reversal) flips parity from that round on — **verified for `reversal_round=0`; >0 is
rule-asserted, re-verify against a real reversal capture.** Turn/clock are DERIVED
(no explicit on-the-clock frame): current pick = last `pick_no`+1.

## Auction — `sleeper_auction_resolve.mjs` → nomination / bid_update / draft_pick / teams_update

| Sleeper event | → contract |
|---|---|
| `draft_updated_by_nomination` (`metadata{nominated_player_id, nominating_slot, highest_offer, timer_end_at}`) | **`nomination`** (id-only; opening bid=`highest_offer`; `clock_ends_at`) |
| `new_draft_offer` (`slot`, `amount`, `player_id`) + `draft_updated_by_offer` (`metadata{offering_slot, highest_offer, timer_end_at}`) | **`bid_update`** (high bidder=`slot`) |
| `player_picked` (now `metadata{slot, amount}`, `picked_by`) | **`draft_pick`** sale (price=`amount`, winner=`slot`, `is_yours`) |
| `settings.budget` − Σ won per slot | **`teams_update`** (budgets derived; not pushed per-team) |

Clock is an absolute `timer_end_at` (ISO) carried as `clock_ends_at`; the content
script converts to `seconds_remaining`/`MM:SS` at post time (keeps resolvers pure).

## Format gate / cross-poller
In-band: `draft_updated_*.type` = `snake|auction|linear` (linear → snake path). The
poller locks the format on the first format-bearing frame (the join reply). Platform
isolation is by **host** (manifest) — Sleeper only injects on sleeper.com/app; the
resolvers emit nothing for non-`draft:` frames (presence/heartbeat/garbage), asserted.

## Fixtures / tests
`extension/test/fixtures/sleeper/{snake,auction}.json` — real captured Phoenix
frames. Tests replay them through the resolvers (snake_pick/serpentine/status/turn;
bid/nomination/sale/teams_update + self-win), plus parser + cross-poller. Backend:
`find_by_sleeper_id` precedence. Contract emitted verbatim → backend/frontend
otherwise untouched.

## Open / behavior-over-time (prod-only)
- `reversal_round>0` serpentine — re-verify against a 3rd-round-reversal capture.
- Anti-snipe: whether late bids extend `timer_end_at` (clock source is correct
  regardless; the extension behavior is unverified live).
- Mid-draft JOIN: the `phx_join` reply carries full state + all prior picks — relied
  on for config-before-first-pick; verify a reconnect mid-draft replays cleanly.
