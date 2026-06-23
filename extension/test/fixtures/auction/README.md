# Auction draft — real captured Yahoo DOM fixtures

These are the **regression net** for the React-client auction resolver
(`yahoo_auction_resolve.mjs`). They must be **real captured Yahoo `outerHTML`**,
not hand-mocked — so the resolver can be re-verified after Yahoo's next deploy
(the `_ys_*` hash classes rotate every time).

Capture the **React root** (`#main-0-DraftClientBootstrap-Proxy`) `outerHTML` per
state and save here with these exact names (the skipped tests in
`yahoo_auction_resolve.test.mjs` are named per file and un-skip once present):

| File | State |
|------|-------|
| `lobby.html` | room loaded, no nominee (gate + `.ys-team` teams + your-team self-id) |
| `nomination.html` | a player on the block — bid + timer + "N until your turn" (N>0) |
| `your-turn.html` | your turn to nominate (the N=0 / your-turn-now wording) |
| `post-pick.html` | right after a sale (winner reflected in team budgets) |
| `draft-complete.html` | post-draft summary **only if** it shares the root (seeds the negative gate) |

Optional extras (cheap): an opponent `.ys-team` card + your own card; a couple
Picks/Results rows for the future reconciliation pass.

Tuning of the name/bid/clock/bidder resolvers is intentionally HELD until
`nomination.html` lands (the lobby has no nominee/clock/bid to tune against).
