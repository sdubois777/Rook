# Handoff — Yahoo Fantasy API access is gated; what it takes to get it back

Written 2026-07-30. Everything in §1 is measured against the live Yahoo API, not inferred.

---

## AMENDED 2026-07-30 — read `yahoo_api_access_application.md` alongside this

§1 (the gating itself) stands unchanged and is still the measured record. Three claims
elsewhere in this document are **wrong** and were corrected after reading Yahoo's live
terms and re-reading the code. They matter because they would have gone into the
access application verbatim:

1. **"`backend/integrations/yahoo_api.py` is the only call path" (§2) is false.**
   `backend/integrations/yahoo_league_api.py` is a second Fantasy API client, live in
   prod via `platform_factory.py:27`. It has **no `_raise_for_yahoo_auth` guard**, and
   `get_draft_picks` (:226-235) **swallows 403 as "no draft history"** — so a gated
   Yahoo league imports *successfully with an empty draft* rather than erroring. PR #417
   does not cover this path. Add it to the §3b re-verification list.
2. **"reads a user's own leagues" is not enforced by Rook.** `get_all_user_leagues()`
   brute-force probes league IDs across game keys 2016-2026 (other people's leagues),
   `POST /leagues/connect/yahoo` accepts a `league_key` from the client body unchecked,
   and `league_sync.py:159-188` continues the sync when the user owns no team in the
   league — despite a docstring claiming it "fails LOUD". Do not claim membership
   verification in the application.
3. **Attribution (§3a) is the third-biggest obligation, not the first.** The binding YDN
   API Terms carry a **24-hour deletion rule** for Yahoo user data (Rook persists
   indefinitely, with no purge on disconnect) and prohibit **deriving income** from API
   use without prior written permission (Rook is paid; Yahoo sells Fantasy Plus). Both
   need Yahoo to say yes in writing. Neither is fixable by better wording.

Also: §3a's premise that the draft board blends Yahoo prices with Rook valuations is
**not true today** — every DraftBoard cell is Rook- or FantasyPros-sourced. The real
exposure is elsewhere, and it is worse: one league's Yahoo auction prices are written to
**global, non-user-scoped** columns and served to every user, including ESPN- and
Sleeper-only users, and into a third-party LLM prompt. See §4 of the application doc.

---

## 1. What happened

**Yahoo moved the Fantasy Sports API behind an application-and-approval process and
withdrew access from apps that had it under the old self-serve model.** Rook is one of
those apps. Nothing in this codebase caused it and nothing in this codebase can fix it.

Evidence, all reproducible:

| check | result |
|---|---|
| OAuth authorize → consent → callback → token storage | **all succeed** |
| token refresh (client id + secret) | **succeeds** |
| `GET /fantasy/v2/users;use_login=1/games;game_codes=nfl/leagues` | **403** |
| `GET /fantasy/v2/game/nfl` — no user data, just the game resource | **403** |
| legacy `.env` `YAHOO_REFRESH_TOKEN` (worked May, untouched code path) | **403** |
| brand-new grant after revoking Rook at Yahoo and re-consenting | **403** |

Yahoo's body on every one: `"This application is not authorized to perform this action."`

Two further confirmations:

- `developer.yahoo.com/fantasysports/guide/` now **redirects** to
  `sports.yahoo.com/developer/`, which describes a three-step *request access* flow
  (submit → review → approval) and links an application form at
  `https://sports.yahoo.com/developer/access/`.
- A **newly created** Yahoo app is not offered a Fantasy Sports permission at all — only
  OpenID Connect (Email, Profile). The checkbox still visible on the old app is vestigial.

The working grant dates from 2026-05-16, so access was withdrawn sometime after that.

**ESPN and Sleeper are entirely unaffected.** This is Yahoo-only.

---

## 2. Getting access

Apply at **https://sports.yahoo.com/developer/access/**. The form asks for name, business
title, email, and information about the organisation, the product, and use cases.

Things worth knowing before submitting, from Yahoo's own terms on that page:

- **One account per developer.** Creating multiple accounts, or using automation to do
  so, is explicitly prohibited. Do not apply twice or from two identities.
- **Attribution is mandatory if approved** — see §3, it is real product work.
- Yahoo throttles or limits access for usage it considers excessive.
- No reverse-engineering, decompiling, or separating the underlying data. Worth reading
  against how Rook stores `league_auction_history` and player rows before answering the
  use-case questions.

Describe the actual product: a multi-user fantasy football SaaS that reads a user's own
leagues, rosters, and draft results to produce valuations and draft recommendations. The
API is read-only for us — `_api_get_with_token` is the only call path and there is no
write anywhere.

**There is no ETA and no appeal path documented.** Plan for Yahoo being unavailable
through the 2026-08-29 drafts.

---

## 3. Work that lands only if approved

### 3a. Attribution — required, not optional

Yahoo requires "Fantasy data provided by Yahoo Fantasy" displayed within the product,
linking back to Yahoo Fantasy, plus the official logo wherever Yahoo-sourced data is
shown. The branding rules are strict: correct colours only, no rotation, inversion,
recolouring, shadows, strokes, effects, proportion changes, added graphics, or
combination with other marks.

That is a design task, not a config flag, and it interacts with a real problem: **Rook
blends Yahoo-sourced data with its own valuations.** Decide what counts as "displaying
API information" — the draft board shows our `ai_bid_ceiling` next to a market price
partly derived from league history. Get that boundary right before shipping, because the
attribution obligation attaches to the surfaces showing their data.

Surfaces to audit: DraftBoard, PlayerDetailPanel, TeamDetail, Dashboard, and anything
rendering `league_auction_history`.

### 3b. Re-verify the whole connect flow

Several real bugs were fixed while diagnosing this and **none has been exercised against a
working Yahoo API** — the 403 blocks the flow immediately after consent. All are on
`feature/snake-default-and-leaner-card` (PR #417), unmerged at time of writing:

- The `/api` prefix (commit `fba1f78`, 2026-05-21) moved every router, so
  `/auth/yahoo/callback` silently became the SPA catch-all — it returned **200 with
  index.html**, meaning Yahoo's `?code=` landed on the frontend and the token exchange
  never ran. Fixed with a 307 alias at the un-prefixed path. **This broke Yahoo league
  connection for every user from 05-21 and is independent of the access gating.**
- The alias is a redirect, not a second handler, because the CSRF nonce cookie is scoped
  `Path=/api/auth/yahoo` and would not be sent to the un-prefixed path (`missing_binding`).
- The authorize query was hand-joined rather than percent-encoded.
- A startup check (`backend/core/oauth_config_check.py`) now fails loudly when
  `YAHOO_REDIRECT_URI`'s path does not match the mounted callback route.

Once access is granted, walk the full flow end to end and confirm: consent → callback →
tokens stored → `/api/auth/yahoo/leagues` returns leagues → import → the league appears in
the sidebar and drives format resolution.

### 3c. Redirect URIs

The registered list must contain the **`/api`** path. Currently registered on the old app:

```
https://rookff.com/api/auth/yahoo/callback                                  correct
https://localhost:8000/auth/yahoo/callback                                  works via the alias
https://fantasymanager-production.up.railway.app/auth/yahoo/callback        missing /api
```

If a **new Yahoo app** comes out of the approval process, register:

```
https://rookff.com/api/auth/yahoo/callback
https://localhost:8000/api/auth/yahoo/callback
```

and update `YAHOO_CLIENT_ID` / `YAHOO_CLIENT_SECRET` in both `.env` and Railway.

### 3d. Local dev needs HTTPS

Yahoo refuses to register an `http://` redirect URI, so the dev callback must be
`https://localhost:8000/...` and the dev server has to speak TLS or the browser reports
*"SSL received a record that exceeded the maximum permissible length"* — which looks like
a Rook bug and is not. `scripts/make_dev_cert.py` writes a self-signed cert to a gitignored
`certs/`; run uvicorn with `--ssl-keyfile/--ssl-certfile` and start Vite with
`VITE_API_TARGET=https://localhost:8000`. Accept the cert warning at
`https://localhost:8000/health` once, or the callback dies on arrival.

---

## 4. Work worth doing NOW, without waiting

- **Gate the Yahoo option in the league-setup wizard** (`frontend/src/pages/LeagueSetup.jsx`,
  `PLATFORMS` at the top). Right now a user can pick Yahoo and walk into a flow that
  cannot succeed. Prefer disabling it with an explanatory note over letting them discover
  it at the end. Not done — deliberately left as a product call.
- **Prod is still 500ing** on `/api/auth/yahoo/leagues` with a raw stack trace, because
  the clean error mapping is on the unmerged PR. Releasing just the Yahoo/OAuth commits
  would turn that into the message in §5.
- `_warn_unusable_auction_rows` and the auction identity backfill (PR #415, merged) assume
  Yahoo sync works. Prod's 2025 auction rows are still unrepaired; the backfill has only
  been rehearsed on dev.

---

## 5. What the code does today

`_raise_for_yahoo_auth` (`backend/integrations/yahoo_api.py`) separates the two failures,
because their remedies are opposite:

- **401** → the user's grant died. `400` + `action: connect`. Reconnecting works.
- **403** → the API is gated and Rook is not approved. `502`, `reason: api_access_pending`,
  and a message that says so plainly and points at ESPN/Sleeper. It deliberately does not
  suggest reconnecting, because that wastes the user's time.

Before this, every Yahoo refusal surfaced as a bare 500 with a stack trace — `httpx`'s
`raise_for_status()` also discards the response body, which is where Yahoo puts the actual
reason. That is why this took a long time to identify.

---

## 6. Traps

- **Do not diagnose this from the Yahoo console UI.** The old app still displays a ticked
  "Fantasy Sports · Read" box while every call 403s. The checkbox is not the truth.
- **A 302 from Yahoo's authorize endpoint proves nothing.** Success redirects to the
  login/consent screen; failure redirects to `/oauth2/error?...`. Read the destination.
- **Yahoo silently re-issues an existing grant** rather than re-prompting, so "disconnect
  in Rook and reconnect" does not produce a fresh consent. Only revoking Rook in the
  user's Yahoo account connections does.
- **`developer.yahoo.com/fantasysports/guide/` redirects.** Any older doc or StackOverflow
  answer describing self-serve Fantasy access predates the gating.
- The client secret for the throwaway `RookTest` app was shared in a chat transcript on
  2026-07-30. Rotate or delete that app.
