# Yahoo Fantasy API — access application, and what approval actually obliges

Written 2026-07-30. Companion to `yahoo_api_access_handoff.md`, which established
*that* access is gated. This establishes *what applying commits Rook to*, and drafts
the application.

Everything about Yahoo's requirements below was read from the live pages on
2026-07-30 (`sports.yahoo.com/developer/`, `/developer/access/`, the YDN Attribution
Policy, and the YDN API Terms of Use). Everything about Rook was read from the code.

---

## 0. The headline

**Attribution is the third-biggest problem, not the first.** The handoff scoped §3a
around logos and the string "Fantasy data provided by Yahoo Fantasy". That work is
real, and §3 below designs it. But two clauses in the binding terms are more
consequential, and one of them is a build-blocker:

| # | Clause | Rook's position today |
|---|---|---|
| 1 | **24-hour deletion.** Yahoo user data not "explicitly identified as being storable indefinitely in the API Documents" must be removed "within 24 hours after the time at which you obtained the data". No such designation exists anywhere in the Fantasy Sports docs. | Rook persists league settings, `manager_map`, and `league_auction_history` **indefinitely**, with no TTL, no revalidation, and **no purge on disconnect**. Directly incompatible. |
| 2 | **Commercial use needs written permission.** Prohibited to "derive income from the use or provision of the Yahoo APIs... unless Yahoo gives prior, express, written permission." Separately, no use "in a product or service that competes with products or services offered by Yahoo". | Rook is a paid subscription product. Yahoo sells **Fantasy Plus**, a paid fantasy-analysis product. Both clauses are live. |
| 3 | **Attribution + branding.** String, logo, placement, colour rules. | Not built. §3 below. |

Neither #1 nor #2 is fixable by writing a better application. Both need Yahoo to say
yes in writing. The good news: **the form has an "Additional Notes" field that Yahoo's
own page points you to for exactly this kind of request.** Ask up front. Being told
"no" now is much cheaper than being found non-compliant after launch, when the remedy
is unilateral termination ("Yahoo may terminate the license at any time for any
reason").

There is also a fourth problem that is Rook's alone, described in §4: **one league's
Yahoo auction prices are written to global, non-user-scoped columns and served to every
user, including ESPN- and Sleeper-only users, and into a third-party LLM prompt.** That
is a cross-tenant data-handling defect independent of Yahoo, and it makes several
sentences you would naturally write in an application untrue.

---

## 1. Corrections to the handoff

Three statements in `yahoo_api_access_handoff.md` are inaccurate. They matter because
they would have gone into the application verbatim.

**1a. "`backend/integrations/yahoo_api.py` is the only call path" — false.**
There are two modules that call `fantasysports.yahooapis.com/fantasy/v2`:
- `backend/integrations/yahoo_api.py:38` (`_api_get_with_token` :291, `_api_get` :311)
- `backend/integrations/yahoo_league_api.py:29` — its **own** `_YAHOO_API_BASE` and its
  own `_api_get` (:137). Live in production via `platform_factory.py:27`. Two callers:
  `get_rosters` (:157) and `get_draft_picks` (:223).

Consequences beyond wording:
- `yahoo_league_api.py` has **no `_raise_for_yahoo_auth` guard**, so the clean 403
  message from PR #417 does not cover the league-sync path.
- Worse, `get_draft_picks` **swallows 403 as "no draft history"**
  (`yahoo_league_api.py:226-235`) and returns `[]`. Under the current gating a Yahoo
  league therefore imports *successfully with an empty draft*, rather than erroring.
  That is a silent-wrong-answer failure, and it is not fixed by PR #417.

**1b. "read-only" is true of the Fantasy API, but needs precise wording.**
Every request to `fantasysports.yahooapis.com` is `client.get` — verified across all
19 call sites; none of the three GET helpers accepts a `data=`/`json=`/`content=`
kwarg, so no caller can inject a body. There is no roster set, add/drop, trade, waiver
claim, bid, or pick submission anywhere. The extension makes **zero** requests to any
Yahoo host (verified in `extension/dist/` too), and `ws_interceptor.js:13-26` only
listens inbound — it never overrides `send()`.

But there *are* four POSTs to a Yahoo *host*: `api.login.yahoo.com/oauth2/get_token`
(`yahoo_api.py:122, :152, :184` and `yahoo_league_api.py:110`) — OAuth code exchange
and token refresh. Say "no writes to the Fantasy Sports API; the only non-GET requests
to any Yahoo host are OAuth token-endpoint POSTs", not "no POST to Yahoo".

Also worth stating proactively: `DELETE /auth/yahoo/disconnect`
(`backend/routers/auth.py:319`) deletes the local credential only — **Rook never calls
Yahoo to revoke the token.**

**1c. "reads a user's own leagues" — not enforced by Rook.**
- `get_all_user_leagues()` (`yahoo_api.py:504-517`) brute-force **probes**
  `{game_key}.l.{league_id}` across every game key 2016–2026. Yahoo league IDs are
  unique only *within* a game key, so the other candidates address unrelated people's
  leagues, and Yahoo serves public leagues to any authenticated user. It accepts the
  first that resolves.
- `POST /leagues/connect/yahoo` (`league_connect.py:127-172`) takes `league_id` /
  `league_key` from the client body with **no server-side check** against the
  `users;use_login=1` listing.
- `league_sync.py:159-188` — when the user owns no team in the league, the sync
  **continues** and stores every team's roster, manager names, and up to three seasons
  of draft picks. The docstring at :39 claims it "fails LOUD". It does not.

The correctly-scoped endpoint (`get_user_leagues`, `yahoo_api.py:711`, using
`users;use_login=1`) does exist and is what the wizard uses. But Rook performs no
independent membership verification, and the application must not claim it does.

---

## 2. The application

Yahoo's form is **twelve short fields, mostly single-line `<input>`s** — not an essay.
Note `companyDescription` and `useCase` are single-line inputs, not textareas, so keep
them tight. `additionalNotes` shows no asterisk but carries `required=true` in the DOM.

Fill in the bracketed values before submitting. **One account per developer — do not
submit twice.**

| Field | Value |
|---|---|
| Name | `[Stephen Dubois]` |
| Business Title | `[your title — Founder / Director]` |
| Email | `rookadmin@rookff.com` |
| Phone Number | `[phone]` |
| Business Name & Address | `[REGISTERED ENTITY NAME], [full postal address]` — one line, org **and** address |
| Consumer-Facing Product or App Name | `Rook` |
| Brief Company Description | `[ENTITY] operates Rook (rookff.com), a subscription fantasy football analytics product that builds independent player valuations from public NFL data and helps managers run their own leagues.` |
| Website URL | `https://rookff.com` |
| Expected Users | `Small (< 1,000 users)` |
| App ID | `[existing YDN App ID for the Rook app, if you want it reinstated rather than reissued]` |

**Describe Your Intended Use Case** (single line — this is the one to get right):

> Read-only access to a signed-in user's own Yahoo Fantasy Football leagues — league
> settings, rosters, and draft results — so Rook can tailor its independently-computed
> player valuations and draft guidance to that user's league format, and show them
> their own league's history. No data is written back to Yahoo and no Yahoo data is
> resold or redistributed.

**Additional Notes** — this is where the two blockers go. Yahoo's own page directs
unusual requests here:

> Rook is read-only; we need no write access.
>
> Three things I want to raise up front rather than assume:
>
> 1. **Data retention.** The YDN API Terms require removing Yahoo user data within 24
> hours unless the API documents identify it as storable indefinitely, and I can't find
> such a designation in the Fantasy Sports documentation. Rook's core function is
> keeping a user's league in sync across a season, which needs league settings, rosters
> and draft results to persist for as long as that user keeps their league connected.
> We delete on disconnect and on account deletion. Could you confirm whether
> user-initiated league sync is covered, or tell me the retention limit you want us to
> hold to?
>
> 2. **Commercial use.** Rook has paid subscription tiers. The terms prohibit deriving
> income from use of the APIs without prior written permission, so I'm requesting that
> permission explicitly. Users pay for Rook's own valuation and draft-analysis engine,
> which is built from public NFL data; Yahoo API access is a convenience for importing
> a league the user already owns, and is not itself a paid feature.
>
> 3. **Live draft assistance.** Rook has a browser extension that reads a user's own
> Yahoo draft room in their browser to give real-time advice during the draft, because
> the Fantasy API has no live draft feed. It makes no requests to Yahoo servers and
> sends nothing to Yahoo. I'd rather disclose this now than have it surface later — if
> you'd prefer we not do it, tell me and we'll remove the Yahoo draft-room support.
>
> Attribution: we'll carry "Fantasy data provided by Yahoo Fantasy" with the official
> logo, linking back to Yahoo Fantasy. Two questions there — see below if useful.

### Why disclose the extension (§3 of your answer)

You chose to disclose it plainly, and the code supports a clean disclosure: the
extension makes **zero network requests to any Yahoo host**, reads only the user's own
draft room in their own browser, and never calls `send()` on Yahoo's WebSocket. It is
closer to a browser accessibility tool than to scraping a server. It is also already
public in your privacy policy and will be public in the Chrome Web Store listing, so a
reviewer can find it — volunteering it costs nothing and buys credibility for the two
harder asks above.

### Things the form does *not* ask for

No API call volume, no architecture, no data-retention plan, no privacy-policy URL, no
business model. Don't volunteer more than the notes above.

---

## 3. Attribution — where the boundary falls

### The rule

> **A surface must carry attribution if any value on it would be absent, different, or
> wrong had this user never connected Yahoo — unless Yahoo's contribution is only to
> *select, filter, order, or parameterize* an otherwise Rook-computed display.**

Three questions, in order:
1. Would the rendered value change if the Yahoo connection vanished? No → exempt.
2. If yes: is Yahoo's contribution *the value itself* (verbatim, formatted, or
   arithmetic on it), or *which Rook value is shown*? Verbatim/arithmetic → attribute.
   Selection/routing → exempt.
3. A Yahoo number baked offline into a global constant shipped to every user on every
   platform is a model parameter, not API information → exempt from attribution, but
   see §4 — that path needs a fix, not a logo.

The rule is phrased as **necessary input, not rendered pixel** because of the waiver
free-agent pool: it displays no Yahoo number at all, yet its *membership* is computed
by subtracting every Yahoo-rostered player from the universe. A "does a Yahoo value
appear on screen?" test would wrongly exempt it, and that is exactly the surface an
adversarial reviewer points at.

### Asymmetry that resolves most hard calls

**Err toward attributing at the page level; err away from attributing at the cell
level.** Page-level over-marking costs a line of chrome and buys goodwill in a review.
Cell-level over-marking plants Yahoo's mark on Rook's differentiated valuation IP,
makes the ESPN/Sleeper product look Yahoo-powered, and runs straight into the clause
most likely to bite: *"Do not combine the Yahoo Fantasy word marks and logos with other
brands and/or marks."*

### Requires attribution

| Surface | Why |
|---|---|
| **League setup wizard, Yahoo branch** (`LeagueSetup.jsx:78, :141, :237, :606`) | Every field is straight from `/auth/yahoo/leagues` and `/auth/yahoo/league-settings`. Nothing Rook-derived. The unambiguous case. |
| **Trade page** (`Trade.jsx:240, :242, :259`) | Team names *and* roster membership from `YahooLeagueAPI.get_rosters`. **Not in the handoff's list** — the strongest case after the wizard. |
| **Matchup page** (`matchup.py:209, :218-222, :248, :298`) | `my_team_name`, `opponent_team_name`, opponent roster — all Yahoo. |
| **Waiver page** (`waiver.py:56, :168`; `Waiver.jsx:123`) | Team names, plus the free-agent pool derived by subtraction. |
| **Account page league cards** (`Account.jsx:269, :282, :504-506`) | `team_names` is `manager_map.values()` — **every manager's name in the league**. The most sensitive Yahoo data in the product. |
| **Sidebar LeagueSelector** (`LeagueSelector.jsx:71-73, :90, :109`) | League name, draft type, scoring, team count — all Yahoo-synced. Persistent chrome, so one mark here discharges the app shell. |
| **Dashboard countdown banner** (`Dashboard.jsx:27-40, :130-147`) | Renders `draft_date` from Yahoo `settings.draft_time`. Arithmetic over a Yahoo value is still display of a Yahoo value. |
| **Live draft room** (`NominationPanel.jsx:92-112`, `TeamRosterPanel`) | Extension-sourced, *not* Fantasy API — so the clause doesn't strictly reach it. Attribute anyway: arguing that scraping their draft room carries *fewer* obligations than using their sanctioned API is not a position to take in a review thread. |
| **League Tendencies** — not yet built (`api/league.js:3` has no consumer) | Would render manager names and verbatim auction prices. Most exposed surface in the product if it ships. |

### Exempt

- **DraftBoard player grid** — every cell is Rook-computed or FantasyPros. The two
  Yahoo inputs (`team_count` sizing round groups, `scoringFormat` picking a column) are
  pure selectors. The giveaway: the page renders identically for a Sleeper user and is
  **fully reachable with no league connected at all**. Note `$200 / $185` at
  `DraftBoard.jsx:535-536` are hardcoded literals — Yahoo exposes no budget field
  (`yahoo_api.py:936-938`), so budget is Rook's invention and *cannot* be
  Yahoo-attributed.
- **PlayerDetailPanel** — exempt **today, conditionally**. `league_bias` /
  `league_bias_signal` are returned by `players.py:502-503` but no component reads
  them, and `signals.js:14` reads `market_value_league`, which the response schema
  never returns. The exemption is one backend field-addition away from being false.
  This is why enforcement must live at the **API schema layer**, not in JSX review.
- **Teams / TeamDetail** — zero Yahoo input. Permanently exempt.
- **Dashboard cards other than the countdown** — RSS, `/players`, `/players/summary`.
  The *page* carries attribution because the banner sits on it; that is page-level
  inheritance, not a per-card obligation.
- **All marketing surfaces** — `Landing`, `Pricing`, `/privacy`, `/terms`. The FAQ's
  plain-text "Yahoo Fantasy, ESPN, and Sleeper" is nominative naming of a supported
  platform, not display of API data. **Do not put a Yahoo logo on the marketing site** —
  it implies a partnership that does not exist.
- **Every surface for an ESPN-only or Sleeper-only user** — exempt by construction, if
  the component keys on `selectedLeague.platform`.

### Placement

1. **App shell** — `<PlatformAttribution variant="bar" />` in `Sidebar.jsx` (~:134),
   *not* `Layout.jsx`. The sidebar already renders `LeagueSelector`, so the mark sits
   physically next to the league it attributes, and inherits its own whitespace — which
   is what keeps it out of a "combined with other brands" strip. Renders only when
   `selectedLeague.platform === 'yahoo'`; switching to a Sleeper league makes it vanish.
2. **Surface-local** — the Yahoo wizard branch (no `selectedLeague` in context yet, so
   pass `platform="yahoo"` explicitly) and the `DraftRoom.jsx` header (~:129).
3. **Column footnote, never a cell badge** — a superscript dagger on
   `SortableHeader.jsx` plus **one** footnote per table. `DraftBoard` has zero Yahoo
   columns today, so this ships as scaffolding — which is the point: it makes "add
   `market_value_league` to the board" a five-minute compliant change instead of a
   violation.
4. **Static page** — an Attribution section in `docs/business/rook-terms-of-service.md`
   (already server-rendered at `/terms`, `main.py:398`) covering Yahoo, ESPN, Sleeper,
   FantasyPros and nflverse, linked from `LandingFooter.jsx`. This is what a reviewer
   finds fastest.

**Do not** add a Yahoo logo to the platform picker at `LeagueSetup.jsx:9`. It uses an
emoji today; leave it. Three platform logos in a row is precisely the prohibited
combination.

### The asset, and dark mode

Yahoo publishes **exactly one** Yahoo Fantasy asset — a single-fill `#7d2eff` SVG at
`763445962456-brand-assets.s3.us-west-2.amazonaws.com/brandwebsite/s3fs-public/Yahoo_Fantasy.svg`
(2,925 bytes). There is **no white, black, or reversed variant**, and recolouring,
strokes, shadows and effects are all explicitly prohibited. Rook's shell is dark.

Resolution: render the unmodified SVG on a small light plate
(`bg-white/95 rounded px-2 py-1`). Putting an unaltered mark on a background plate is
not a modification of the mark; recolouring it to white is, and inventing a variant
Yahoo has not published is worse.

Vendor the file to `frontend/public/vendor/yahoo/` and commit it — do not hotlink S3
(CSP `img-src` exception, and the asset can change underneath you) and do not inline it
as JSX (the first developer who wants it to match the theme will set
`fill="currentColor"` and silently commit a branding violation; a static file resists
that). Drop a `README.md` beside it quoting the seven prohibitions.

### Anti-rot: mark the schema, not the JSX

The single most valuable piece of this design. Annotate Yahoo-provenanced response
fields with `Field(json_schema_extra={"provenance": "yahoo"})` — `market_value_league`,
`league_bias`/`league_bias_signal` (`players.py:188-189`), `BiasPlayer.market_value_league`
(`league.py:50`), the `_league_response` passthroughs (`account.py`), and the team-name
and roster fields in `trade.py` / `matchup.py` / `waiver.py`. Then add a test that walks
every registered response model, collects the provenance-marked fields, and asserts the
set equals a checked-in allowlist.

Adding a Yahoo-sourced field to any API then **fails CI** until someone edits the
allowlist, and the review question attached to that edit is "does the surface consuming
this render the attribution?". That converts a legal obligation into a failing test
instead of tribal knowledge.

### Two attribution questions genuinely need Yahoo's answer

Yahoo publishes **two conflicting requirements** and has not reconciled them:
- The Fantasy page: *"Fantasy data provided by Yahoo Fantasy"*, linking to Yahoo Fantasy.
- The YDN Attribution Policy — which the binding API Terms of Use **incorporates by
  reference** — *"powered by Yahoo"*, linking to `https://www.yahoo.com/?ilc=401`.

Different strings, different destinations. Safest is to satisfy both; but the contract
cites the *other* one, so this is a question for Yahoo, not a silent judgment call.
Likewise: is a persistent app-shell mark plus an attribution page sufficient, or does
"whenever referencing or using... information" mean every screen?

---

## 4. The problem that is Rook's alone

Independent of Yahoo, and it makes several natural application sentences untrue.

**One league's Yahoo auction prices are written to global, non-user-scoped columns and
served to every user.**

- `league_auction.py:338-360` — `_sync_season` inserts `LeagueAuctionHistory` rows with
  **no `user_id` and no `user_league_id`** (both nullable, `league_auction_history.py:36-42`).
- `league_auction.py:457-507` — `refresh_market_value_league` takes `max(season_year)`
  across the **whole table, no user filter**, and writes `players.market_value_league`
  on the shared global table (:503).
- `market_values.py:275-288` — raw SQL `SELECT player_name, AVG(price) FROM
  league_auction_history GROUP BY player_name`, no user filter, writing
  `players.market_value_prior_season` globally.
- `player_profiles.py:1484-1509` — that value is serialized into a **Sonnet prompt** as
  "the model's only view of what the market thinks a player is worth", and
  `market_value_league <= 5` is a routing trigger. So one league's prices steer
  AI-generated profile text on **every** user's board — including ESPN- and
  Sleeper-only users — and are sent to a third-party LLM provider.
- `draft_state.py:44-83` — **`OpponentProfile` has no `user_id` and no
  `user_league_id` column at all.** `build_manager_profiles`
  (`league_auction.py:596-700`) reads all auction history unfiltered, **deletes every
  `OpponentProfile` for the analysis year regardless of owner** (:637), and persists
  verbatim `{name, position, price}` rosters plus **real manager names** (:679-686).
  `draft.py:140-145` injects them into `OpponentThreatAnalyzer` for whoever starts a
  live draft. Manager names and auction spend from one league can surface as opponent
  tendencies inside a different user's live draft.
- `valuation.py:83-88` — `POSITION_BUDGET_SHARE` (QB .083 / RB .385 / WR .456 / TE .076)
  is fitted to **one league's** three seasons of auction history and hardcoded, then
  applied to every user's board.

Only two layers are correctly scoped: `league_auction_repo.py:31-36` and
`league_sync.py:443-446`.

Two YDN clauses sit directly on this: *"You may not disclose any Yahoo user data or
store any Yahoo user data in any data repository that enables any third party (other
than the Yahoo user) access"*, and *"may not... separate its underlying data"*.

**No attribution treatment addresses any of it.** Fix or accept before the application
goes in, because it changes what you can truthfully write. The cheapest partial fix:
scope `refresh_market_value_league` per `user_league`, drop `market_value_league` from
the Sonnet prompt, add `user_league_id` to `OpponentProfile`, and re-derive
`POSITION_BUDGET_SHARE` from the now-multi-platform corpus (ESPN and Sleeper write to
the same table, so this is cheap and removes the Yahoo exposure entirely).

---

## 5. Also true, and cheap to state in the application

- Credentials are Fernet-encrypted at rest (`credential_repo.py:47-73`).
- A public, easily-accessible privacy policy exists at `rookff.com/privacy`,
  server-rendered, and already discloses Yahoo data handling and deletion-on-disconnect.
  **Gap:** the YDN terms require disclosing "the fact that a third party collects,
  stores and uses personal data in connection with your product or service" — the
  current policy does not say this. One sentence to add.
- Terms of service at `rookff.com/terms` already disclaim affiliation with Yahoo
  (`rook-terms-of-service.md:46`).
- `GET /billing/pricing` and the tier model make clear that Yahoo import is not itself
  a paid feature.

---

## 6. Sequence

1. **Rotate or delete the throwaway `RookTest` app** — its client secret was shared in
   a chat transcript on 2026-07-30 (handoff §6).
2. Fill the bracketed values in §2 and submit. One shot, one account.
3. Ship the wizard gate (done — see `LeagueSetup.jsx` / `lib/constants.js`) so users
   stop walking into a dead flow while you wait.
4. Fix §4 — it is a real defect regardless of Yahoo's answer.
5. Only if approved: build §3, then re-verify the whole connect flow end to end
   (handoff §3b), **including the `yahoo_league_api.py` path, which PR #417 does not
   cover and which currently swallows 403 as empty draft history.**
