# Rook — Live-Draft Browser Extension — Directory Guide

> Auto-loads when working under `extension/`. Moved out of the root `CLAUDE.md`
> (July 2026). Broader project status/backlog is in `docs/STATUS.md`.

---

## Live Draft — Browser Extension Architecture (Yahoo / ESPN / Sleeper)

The extension (`extension/`, MV3, sideloaded) reads each draft room and POSTs events
to the backend; the backend enriches + runs the AI rec + broadcasts to the React room
over WebSocket. **One poller per platform/format**, all mapping onto **one backend
event contract** (so backend/frontend are platform-agnostic — a new platform that maps
onto the contract needs zero downstream changes).

**Event contract** (`backend/routers/draft.py` keys on `event.type`, not platform):
- Auction: `nomination`, `bid_update`, `clock`, `draft_pick`, `teams_update`
- Snake: `your_turn`, `your_turn_soon`, `snake_status`, `snake_pick`

**Pollers / resolvers** (pure parse/detect logic in `*_resolve.mjs`, linkedom/JSON-
tested against real captures in `extension/test/fixtures/<platform>/`):
- Yahoo: `yahoo_draft.js` + `yahoo_auction_resolve.mjs`; `yahoo_snake_draft.js` +
  `yahoo_snake_resolve.mjs`. React DOM, shared root `#main-0-DraftClientBootstrap-Proxy`.
- ESPN: `espn_draft.js` + `espn_salarycap_resolve.mjs` / `espn_snake_resolve.mjs` +
  `espn_shared.mjs`. React/styled-jsx, content-gated, no stable root.
- Sleeper: `sleeper_draft.js` (ISOLATED) + `sleeper_draft_main.js` (world:MAIN WS
  interceptor) + `sleeper_resolve.mjs` / `sleeper_snake_resolve.mjs` /
  `sleeper_auction_resolve.mjs`. Phoenix Channels over WS (JSON, no DOM).

**Hard-won universal rules (each cost a prod bug — keep them):**
1. **Content-based cross-poller gate.** When pollers share a page/root, each MUST
   positively detect its OWN format from positive content — host/root presence is
   never a discriminator. Asserted active-on-own + inert-on-others, both directions,
   in the same test pass. (Yahoo auction↔snake share a React root; the test net is
   non-negotiable.)
2. **CSP blocks inline WS interception → use a `world:"MAIN"` content script.** Pages
   (Yahoo, Sleeper) block injected inline `<script>`. A manifest `world:"MAIN"` entry
   at `document_start` is browser-injected (CSP-exempt) and patches `window.WebSocket`
   before the page opens it. Relay frames to the ISOLATED poller via
   `window.postMessage` (CustomEvent `detail` does NOT cross worlds in Chrome).
3. **Each platform's page origin needs a CORS allowlist entry** in `backend/main.py`
   `allow_origin_regex` (bounded, dot-escaped; Starlette uses `fullmatch`). The poller
   posts from the page origin — ESPN (`fantasy.espn.com`) and Sleeper (`sleeper.com`/
   `.app`) each 400'd until added. **Backend change → needs a release to take effect.**
4. **The SPA persistent socket opens at app boot.** Sleeper opens one WS on the lobby
   and joins the draft as a channel — so the interceptor + poller match ALL of the
   site (`sleeper.com/*`), not just `/draft/*`, or it misses early picks.
5. **Orphaned-context recovery.** An extension reload/auto-update orphans the running
   content script (`browser.*` throws "Extension context invalidated"); the poller
   detects a dead `browser.runtime.id` on a draft frame and reloads the tab once
   (capped, reset on healthy relay) to re-inject a fresh poller.
6. **Anchor policy.** `data-testid` + hand-authored semantic classes = PRIMARY;
   build-hash classes (`_ys_*`, `jsx-<digits>`) ROTATE per deploy → FALLBACK ONLY,
   behind a text/structure check, with loud `console.warn` + `selector_health`.
7. **Player resolution: id-first, then name backstop.** Sleeper id → exact
   `players.sleeper_id` (`find_by_sleeper_id`); else name+pos fuzzy
   (`find_by_name_fuzzy`). ESPN/Yahoo surfaces are name-only → name backstop.
8. **`is_yours` is authoritative for own-pick attribution** (slot labels like "Team 5"
   don't equal `your_team_id`); `record_pick(is_yours=...)` routes own buys to
   `your_roster`. Self-team label "You" so the frontend folds it into `myTeamName`.

---

## Known Issues / Backlog — Extension

- CROSS-POLLER RULE (non-negotiable): the snake
  and auction Yahoo pollers SHARE the same URL
  match patterns AND (as of June 2026) the SAME
  React root #main-0-DraftClientBootstrap-Proxy.
  Each MUST positively detect its OWN draft type
  from POSITIVE CONTENT before acting — the shared
  root is NOT a discriminator. Auction content =
  a Proj-$ nominee (structural: a ys-player[data-id]
  whose short text carries "Proj $") OR >=1 .ys-team
  carrying a $-budget span. Snake content = the turn
  banner ("Your Turn • Round R, Pick P" / "{Name}'s
  Pick • You're up in N Picks • Round R, Pick P").
  Gates: shouldAuctionActivate (yahoo_auction_resolve
  .mjs — content-only: NO timer arm, snake has a
  00:xx clock too; NO bare-.ys-team arm, snake's 180
  board cells are budget-LESS) and shouldSnakeActivate
  (yahoo_snake_resolve.mjs). The snake poller is now
  NON-DESTRUCTIVE — it reads the Board view only
  (banner + "Last:" indicator + serpentine board
  grid), no "Picks"-tab click. History: (1) the old
  snake poller's clickPicksTab() ran on auction pages
  and took the auction room down; (2) fixing auction
  then broke snake when both rooms moved to the shared
  root and the auction gate's timer/bare-.ys-team arms
  false-tripped on snake's 180 budget-less cells. Both
  are why the guard is content-positive in BOTH
  directions now.
- STANDING RULE: snake changes MUST be verified
  against AUCTION (and vice versa). This is the
  2nd snake change to break auction (1st: the
  VORP classifier; 2nd: the poller). Tests cover
  BOTH directions so this class is caught in CI,
  not prod — keep it that way.
- AUCTION REACT CLIENT (2026 replatform): Yahoo
  migrated the AUCTION room to a React app, root
  #main-0-DraftClientBootstrap-Proxy, with NO
  semantic selectors on live data. Two class
  families: `ys-*` KEBAB classes (e.g. .ys-team,
  .ys-player) are hand-authored/semantic — OK as
  anchors; `_ys_*` HASH classes are build-
  generated and ROTATE every Yahoo deploy — NEVER
  a primary key, only a fallback layered behind a
  text/structure check, and using one MUST emit
  loud telemetry (console.warn + selector_health
  heartbeat) so a rotation alarms instead of
  silently stalling. Auction selectors must be
  TEXT / STRUCTURE / kebab-`ys-` anchored
  (resolveAuctionState in yahoo_auction_resolve
  .mjs): gate = root + (timer SPAN /^\d{2}:\d{2}$/
  not in a dialog OR >=1 .ys-team) AND NOT draft-
  complete; nominee identity = ys-player[data-id]
  (stable player ID) primary; team self-id =
  <span>You</span> + .ys-team[data-id] primary.
  Fixtures = REAL captured Yahoo outerHTML under
  extension/test/fixtures/auction/ (re-runnable
  after each deploy), parsed with linkedom.
- SNAKE-MIGRATION LANDMINE — RESOLVED (June 2026):
  Yahoo migrated SNAKE onto the shared auction React
  root (#main-0-DraftClientBootstrap-Proxy), exactly
  as predicted. The old `hasAuctionRoot` veto then
  silently disabled snake on its own page, and the
  auction gate's timer / bare-.ys-team arms false-
  tripped on snake (grabbed the 180-cell board as
  "opponents"). Fix: a React snake resolver
  (yahoo_snake_resolve.mjs) + a content-positive
  guard in BOTH gates — the `hasAuctionRoot` veto is
  RETIRED entirely (root presence is never a
  discriminator). The old yahoo_snake_draft_observer
  .mjs (#app innerText + pick-card scan) is deleted.
  SERPENTINE board mapping: pickSlotIndex() reverses
  on even rounds (pick 12 == pick 13 slot in a 12-team
  league); the captures are early Round 1 only, so the
  round-boundary case is asserted from the rule —
  re-verify against a real round-turn capture to lock.
  Fixtures: extension/test/fixtures/auction/snake-
  {onclock,waiting,postpick}.html.
- Passive sync runs for BOTH Yahoo AND ESPN
  (shipped behavior). yahoo_auth.js calls
  triggerPassiveSync('yahoo') on every Yahoo
  fantasy page visit; espn_auth.js calls
  triggerPassiveSync('espn'); the backend
  /leagues/sync-platform/{platform} accepts
  yahoo+espn (30-min debounce). window.__rook__
  detection also works for LeagueSetup. (The
  isolated content script injects fine; page CSP
  only blocks the MAIN-world WS interceptor.)
- my_nomination/my_bid console.error events
  relayed to UI but not yet folded into
  engine DraftStateManager budget/roster
  state. Auto-roster updates and scraped-
  budget reconciliation are future work.
- DOM selectors (#draft, position regex,
  budget line format) confirmed against
  June 2026 mock draft. Re-verify against
  real August draft room — Yahoo may change
  their DOM between now and then.
- Extension not yet published to Chrome
  Web Store or Firefox Add-ons. Sideload
  only (Load unpacked / Temporary Add-on).
