# Rook — Stripe Subscription Billing: Design / Scoping Pass

> **Status: design only. No billing code in this PR.** Deliverable is this doc.
> Decisions are **LOCKED** (see "Approved decisions"). The headline finding: **the
> entitlement + gating layer already exists and is wired for exactly this.** Stripe
> is the missing *billing* half — checkout, a webhook to flip tier, and attaching the
> existing guards to routes. **Security posture (§0) is a hard requirement: card data
> never touches Rook — Stripe Checkout redirect → PCI SAQ-A.**

---

## Approved decisions (LOCKED — June 2026)

| # | Decision | Locked outcome |
|---|---|---|
| 1 | Entitlement source of truth | **Rook DB `users.tier`.** Stripe = billing record, syncs into the DB via webhook; Clerk = identity only. Read on the hot path, reconcile via webhook (§3). |
| 2 | Stale tier doc | **`backend/models/user.py` + CLAUDE.md are canonical** (intro/standard/pro). The stale `stage-25` `free\|starter\|pro\|league` block gets removed in a **separate cleanup PR** (step 7). |
| 3 | `/season` semantics | **Defer `/season` for v1 — ship recurring MONTHLY subscriptions only.** Seasonal can come later once the subscription path is proven. Removes the one-time-pass / expiry-job complexity entirely. |
| 4 | Cancellation | **Period-end, not immediate.** Cancel sets `cancel_at_period_end=true` (Stripe Portal default) → the user **keeps their tier through the period they paid for**. Downgrade to `intro` fires **only** on `customer.subscription.deleted` (which Stripe emits *at* `current_period_end` for a period-end cancel). **Credits persist.** See §4 for the explicit "what does NOT downgrade." |
| 5 | Payment-failed grace | **Honor Stripe's retries** — mark `past_due` on `invoice.payment_failed`, keep access, downgrade only on the terminal `subscription.deleted`. No custom grace logic. |
| 6 | Monthly credit grant | **Webhook-driven, once per paid monthly invoice.** Grant `credits_monthly` **only** on `invoice.payment_succeeded` **AND** `invoice.billing_reason == "subscription_cycle"` (renewals only — see §4 for the non-grant invoices). Idempotency keyed on **`event.id`** (global webhook dedup) **plus** the grant recorded against **`invoice.id`** (one invoice per cycle) so it's provably once-per-invoice. Signup bonus is the separate one-time grant. |
| 7 | Clerk metadata mirror | **No.** DB is the boundary; the SPA reads tier from `/account/me`. |

These collapse the object model to **3 tiers × monthly recurring Price + 3 one-time
credit-pack Prices** — see §6.

---

## 0. Security & PCI posture (non-negotiable)

**Requirement (Stephen): a user must not be able to get their card stolen *through
Rook*.** The way you guarantee that is structural: **card data never touches Rook's
servers, code, logs, or database — at all.**

### A. Card data is handled 100% by Stripe — we never see a PAN
- **Use Stripe Checkout (hosted redirect).** Our backend creates a *Checkout
  Session* (server-side, with our secret key) and returns its URL; the browser
  **redirects to `checkout.stripe.com`**, where Stripe renders and collects the card
  on **their** domain. We never render a card field, never receive card numbers,
  never proxy them. Same for managing/changing a card → **Stripe Customer Portal**
  (also Stripe-hosted).
- **PCI scope = SAQ-A** (the lowest tier — for merchants who fully outsource card
  handling to a PCI-DSS Level-1 provider). We store/process/transmit **zero** card
  data. The only Stripe identifiers we ever hold are **opaque references**
  (`customer_id`, `subscription_id`, `price_id`, `event_id`) — useless to an
  attacker, no cardholder data.
- **Reject Stripe Elements / any in-app card form for v1.** Even though Elements
  tokenizes client-side, it puts Stripe.js on our page (SAQ-A-EP, larger surface).
  Checkout-redirect keeps the card UI off our origin entirely. Use Elements only if a
  future UX demands it, and never let raw card fields post to our backend.

### B. Entitlement integrity — nobody can forge a paid tier
The dangerous class isn't card theft (Stripe owns that) — it's a user granting
*themselves* Pro. Controls:
- **The verified webhook is the ONLY thing that grants entitlement.** Tier is flipped
  exclusively in the signature-verified `/webhooks/stripe` handler.
- **NEVER trust the client success redirect.** The post-checkout `?success=true`
  return URL is user-forgeable (they can hit it without paying) — it may *prompt* a
  refresh but must **never** set `tier`. (Classic SaaS billing vuln.)
- **Webhook signature verification is mandatory** (`stripe.Webhook.construct_event`
  with `STRIPE_WEBHOOK_SECRET`); unverified → 400, no side effects. An unverified
  endpoint is an entitlement-forgery hole.
- **Prices are server-defined.** The checkout endpoint takes a **tier name** and maps
  it to a server-configured `price_id`; the client never supplies an amount or
  price_id. (Prevents "pay $1 for Pro".)

### C. Tenant isolation on the endpoints we own
- `/billing/checkout` + `/billing/portal` require `get_current_user`; the Stripe
  `customer_id` is **bound to the authenticated user** (read from their `users` row),
  never accepted from the request body. You can only ever check out / manage **your
  own** subscription.
- The webhook resolves the affected user by the Stripe `customer_id` stored on the
  `users` row — not by anything in the (already-signature-verified) payload's
  client-influenced fields.

### D. Secret hygiene
- `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` are **server-only Railway env vars**
  (same pattern as Clerk) — never in the repo, never sent to the client, **never
  logged** (scrub from request/error logs). Only a *publishable* key is ever
  client-side, and Checkout-redirect may not even need one.
- **Idempotency keys** on outbound Stripe API calls (checkout creation) so a retry
  can't double-charge.

### E. Operational
- HTTPS only (already — Railway/Clerk). Webhook over HTTPS.
- **Rate-limit** `/billing/*` (checkout/portal creation) to blunt abuse.
- **Audit-log** every tier transition (handled where `upgrade_tier` runs) via the
  existing `RequestLogging`/security middleware — who changed to what, when.
- **PII minimization:** store only the opaque Stripe ids + the email we already have.
  No card data, no billing address held by us (Stripe collects what it needs).

**One-line summary:** redirect to Stripe for anything involving a card; the only
state we keep is opaque ids; entitlement flips solely on a signature-verified
webhook; prices and customer-binding are server-authoritative. That removes card
theft *through Rook* as a possibility and closes the self-upgrade hole.

---

## 1. Tier definition — the source of truth

**Canonical, machine-readable:** `backend/models/user.py:25-77` —
`TIER_LIMITS`, `CREDIT_COSTS`, `CREDIT_PACKS` (the file's own docstring: *"single
source of truth for subscription rules. No other file should define these values."*).
**Prose mirror:** `CLAUDE.md` → "SaaS Pricing (Stages 25-30)".

| Tier | Price | Signup credits | Monthly credits | Leagues | live_draft | trade_analyzer | trade_finder | waiver_wire |
|---|---|---|---|---|---|---|---|---|
| **intro** | $5/mo · $15/season | 25 (one-time) | 0 | 1 | ✗ | ✗ | ✗ | ✗ |
| **standard** | $9/mo · $29/season | 75 | 20 | 2 | ✓ | ✓ | ✗ | ✓ |
| **pro** | $18/mo · $49/season | 200 | 50 | unlimited | ✓ | ✓ | ✓ | ✓ |

The `/season` column is the *eventual* pricing; **v1 ships monthly only** (Decision
#3) — the `/season` Prices come in a later add. `injury_monitoring` + projections /
draft board / news / league sync / draft history are **free on all tiers** (no gate).
**No free tier** — `intro` is the floor.
Credit costs (feature unlocked *then* credits charged): `trade_analysis`=10,
`trade_finder`=20, `waiver_wire`=8 (`user.py:64-70`). Credit packs (one-time):
$5→75, $10→175, $25→500 (`user.py:73-77`). **Live draft is a tier entitlement, not a
credit cost.**

---

## 2. Auth + identity wiring (how it works today)

**Request-auth path** (`backend/core/dependencies.py`):
- `_bearer` → `get_current_user_id` (`:175`): production verifies the **Clerk JWT**
  (`_verify_clerk_jwt`, `:99` — RS256, JWKS fetched + cached). Returns
  `{user_id: <Clerk sub>, email}`. Dev fallback: `X-User-Id` header (`:190-195`).
  Clerk's default JWT has no email → fetched from the Clerk Backend API and cached
  (`_fetch_clerk_user_email`, `:141`).
- `get_current_user` (`:212`): maps `user_id` → DB `User` via
  `UserService.get_or_create(external_id=<Clerk sub>, email)` — **creates the row on
  first request**. So every protected route already has the full `User` (incl. `tier`
  + `credits_remaining`) in hand.

**User model** (`backend/models/user.py:84-140`): `external_id` (Clerk id, unique),
`email`, `tier` (default `"intro"`), `credits_remaining` (default 0, *accumulate,
never reset*), `draft_token` (extension), **`stripe_customer_id`**, **`stripe_subscription_id`**
(both already present, nullable, unique), `deleted_at` (soft delete).

**Clerk custom metadata:** **not used for entitlement today.** The JWT is read only
for `sub` (+ email). Clerk also drives the user lifecycle via a webhook
(`backend/routers/webhooks.py` → `/webhooks/clerk`: `user.created` upsert,
`user.deleted` soft-delete; svix signature verification).

**Identity flow:** `Clerk (sub) → users.external_id → users.tier`. Entitlement
already rides along with the per-request user load — **no extra query, no Stripe
call.**

---

## 3. Entitlement source of truth — the load-bearing decision

Three candidates, evaluated against the existing `Clerk → Rook DB` flow:

| Option | Hot-path read (every gated request) | Pros | Cons |
|---|---|---|---|
| **A. Rook DB `users.tier` (RECOMMEND)** | Already loaded by `get_current_user` — **zero extra cost** | Already implemented + read by the gates; transactional with credits; queryable/admin-able; no third-party on the hot path | Must be kept in sync with Stripe (webhook) — but that's true of any option |
| **B. Clerk `publicMetadata.tier`** | In the JWT claims (no DB hit) if put there | Frontend can read tier off the session | Adds a *second* sync target (Stripe→Clerk→read); JWT is cached/short-lived so changes lag until refresh; not transactional with credits; re-plumbs the gate code that already reads the DB |
| **C. Stripe (query subscription live)** | **Stripe API round-trip per request** | Always "true" | Latency + rate limits on the hot path — **disqualifying**; Stripe is the billing record, not a per-request store |

**Recommendation: A.** `users.tier` is authoritative for *entitlement*; **Stripe is
authoritative for *billing state*** and is the upstream that mutates `users.tier`
via webhook. Clerk stays **identity only**.

**Read vs. reconcile split:**
- **Hot path (read):** `users.tier` (+ `credits_remaining`) — already in the
  `User` object every gated request has. The gate calls
  `FeatureService.check_feature_access(user, feature)` which reads `TIER_LIMITS`
  **in memory** (`backend/services/feature_service.py`). **No Stripe API call ever
  on a request.**
- **Reconcile (webhook, async):** Stripe subscription events →
  `UserService.upgrade_tier` / `apply_signup_bonus` (`backend/services/user_service.py:63-104`,
  whose docstrings already say *"Called by Stripe webhook"*) → write `users.tier`,
  `stripe_*`, credits. The DB is reconciled to Stripe out-of-band; reads never wait
  on it.
- **Keeping the others in sync:** Clerk needs nothing (identity only). If we ever
  add option B as a UX mirror, the same webhook would also push `tier` to Clerk
  `publicMetadata` — strictly secondary, never the gate.

---

## 4. Webhook lifecycle surface

New endpoint **`/webhooks/stripe`** — **root-mounted, NOT under `/api`**, mirroring
the Clerk webhook (`backend/main.py:94`; the Stripe URL Stripe calls is
`https://<host>/webhooks/stripe`). Pattern cloned from `_verify_clerk_signature`
(`webhooks.py:26`), swapping svix for `stripe.Webhook.construct_event(body, sig,
STRIPE_WEBHOOK_SECRET)`.

| Stripe event | Drives (in `users` / via `UserService`) |
|---|---|
| `checkout.session.completed` | First purchase: set `stripe_customer_id` + `stripe_subscription_id`; `upgrade_tier(<purchased>)`; one-time `apply_signup_bonus`. (Use this **or** `subscription.created` as the authoritative "started" signal — not both.) |
| `customer.subscription.created` | Subscription exists. Map `price_id → tier`; set tier if not already set by checkout. |
| `customer.subscription.updated` | **Two distinct cases — keep them separate.** (a) *Tier change* (upgrade/downgrade via price swap, `active`) → `upgrade_tier(<new price's tier>)`. (b) *Cancel scheduled* (`cancel_at_period_end=true`, still `active`) → **DO NOT downgrade**; the user keeps their tier until period end. Optionally surface a "canceling on `current_period_end`" flag for the UI. Also reflect `past_due` status here. |
| `customer.subscription.deleted` | **The only event that downgrades.** Fires when the subscription actually ends — *at* `current_period_end` for a period-end cancel (Decision #4), or immediately for a hard cancel. → downgrade to `intro`, **credits persist**, clear `stripe_subscription_id`. |
| `invoice.payment_succeeded` | **Grant `credits_monthly` ONLY when `invoice.billing_reason == "subscription_cycle"`** (a real renewal). **Do NOT grant** on `subscription_create` (that's the signup → `apply_signup_bonus` path) or `subscription_update` (mid-cycle proration invoice). Idempotency per Decision #6 (event.id + invoice.id). |
| `invoice.payment_failed` | Mark `past_due`; **do not** downgrade yet (Decision #5) — let Stripe retry; terminal failure arrives later as `subscription.deleted`. |

**Timing summary (Decision #4):** the *only* downgrade trigger is
`customer.subscription.deleted`. Cancelling does not delete immediately — it flips
`cancel_at_period_end` and the subscription stays `active` until `current_period_end`,
so honoring only `subscription.deleted` gives **access-through-the-paid-period for
free**. Downgrading on the cancel-scheduling `subscription.updated` would yank access
from someone who already paid for the period — the refund-request generator. **Don't.**

**Requirements (call out explicitly):**
- **Signature verification** — reject unverified payloads (400), same posture as the
  Clerk handler (prod requires the secret; dev may parse unverified).
- **Idempotency (two layers, because credits are real value):**
  1. **Global:** dedup every event by `event.id` — a `processed_stripe_events(event_id
     PK, seen_at)` insert-or-skip — so Stripe's at-least-once *redelivery of the same
     event* is a no-op.
  2. **Grant-level:** the monthly credit grant is additionally recorded against the
     **`invoice.id`** (one invoice per billing cycle) — grant only if that invoice
     hasn't been granted — so it's **provably once per paid monthly invoice**, even if
     two distinct events ever referenced it. Combined with the
     `billing_reason == "subscription_cycle"` filter (which excludes the
     create/update/manual invoices), a user cannot be double-topped. Tier writes are
     naturally idempotent (set, not increment); only the *increment* operations
     (credit grants) need this.

**Test strategy (no staging backend; prod deploys from `main`):**
- **Local first:** `stripe listen --forward-to localhost:8000/webhooks/stripe` +
  `stripe trigger checkout.session.completed` (etc.) against **test-mode** keys.
  Drives the full state machine without touching prod.
- **Post-release smoke:** after releasing to `main`, point a **test-mode** Stripe
  webhook endpoint at the live URL and replay test events; verify `users.tier`
  transitions. Keep test-mode and live-mode endpoints/secrets separate so test
  traffic can't mutate real entitlements. (There is no separate staging service —
  this two-step is the substitute.)

---

## 5. Gate enforcement points

**Enforcement is backend; the pattern already exists — do not scatter checks.**
`backend/core/dependencies.py:281-331`:
- `require_feature(feature)` → `FeatureService.check_feature_access` → 4xx
  `FeatureNotAvailableError` if `TIER_LIMITS[user.tier][feature]` is false.
- `require_credits(action)` → checks feature access **then** `CreditService.deduct`
  (402 `InsufficientCreditsError`).

**These guards are built but NOT yet attached to routes** — only `NOTE` placeholders
in `draft.py:17,545`. Mapping to attach next pass:

| Surface | Tier rule | Enforcement |
|---|---|---|
| `POST /api/draft/start` (live draft) | standard+ (`live_draft`) | `Depends(require_feature("live_draft"))` — the noted-but-unattached gate |
| Trade analyze (router unbuilt) | standard+, 10 cr | `Depends(require_credits("trade_analysis"))` |
| Trade finder (unbuilt) | **pro only**, 20 cr | `Depends(require_credits("trade_finder"))` |
| Waiver wire (unbuilt) | standard+, 8 cr | `Depends(require_credits("waiver_wire"))` |
| `POST /api/account/leagues` | intro 1 / std 2 / pro ∞ | **already gated** via `FeatureService.can_add_league` (`account.py`) |
| projections / draftboard / news / sync / injury | all tiers | no gate |

**Frontend (UX only, not the security boundary):** `frontend/src/pages/Pricing.jsx`,
`components/landing/PricingTable.jsx`, `pages/Account.jsx` (reads `/account/me` →
`tier` + `tier_limits` + `credits`). Needs: upgrade CTAs wired to checkout,
locked-feature affordances, and 402/feature-error → upgrade-prompt handling. None of
these enforce anything — the backend dependency does.

---

## 6. Stripe object model

**Products/Prices (Decision #3 = MONTHLY recurring only; `/season` deferred):** model
**3 Products** (`Rook Standard`, `Rook Pro`, and `Rook Intro`), each with **one
recurring monthly Price** (`recurring{interval:month}`). That's it for subscriptions
— **3 monthly Prices**, no annual/seasonal Price, no expiry job. **Credit packs** are
**3 one-time Prices** (`small/medium/large`), sold via a separate **Checkout
`mode=payment`** (not subscriptions). Subscriptions use **Checkout `mode=subscription`**.
(`/season` is a later add: a new annual recurring Price per tier + a renewal-credit
tweak — no architectural change, so deferring costs nothing.)

- **`price_id → tier` mapping lives in code/config**, not hardcoded in handlers, so
  the webhook resolves a subscription's price to a tier.
- **All card collection is via Checkout redirect** (§0.A) — no card UI on our origin.
- **Test vs live mode:** fully separate object graphs and keys — `sk_test_*` /
  `whsec_*`(test) vs `sk_live_*` / `whsec_*`(live), and **different `price_id`s per
  mode**. Selected by environment (the `STRIPE_*` env vars differ per Railway
  environment); no mode branching in code beyond reading the env.

**Secrets / key handling** — same pattern as Clerk (`backend/config.py`:
`pydantic-settings` `BaseSettings` from env; `.env.example` documents names; real
values are **Railway env vars**, never in the repo). Add:
- `STRIPE_SECRET_KEY` (server SDK)
- `STRIPE_WEBHOOK_SECRET` (signature verification)
- `STRIPE_PRICE_*` (the per-tier/per-period + per-pack price ids)
- `VITE_STRIPE_PUBLISHABLE_KEY` — **likely unnecessary** with Checkout-redirect
  (no Stripe.js on our page); include only if a later UX needs it.

The `stripe` Python SDK is **not yet a dependency** — adding it is part of the next
pass, not this doc.

---

## 7. Proposed implementation sequence (next pass — not now)

1. **Config + SDK:** add `stripe` dep; `STRIPE_*` settings in `config.py` +
   `.env.example`; create **test-mode** Products + 3 monthly Prices + 3 pack Prices;
   record price ids in config.
2. **Billing router** (`/api/billing`, auth-required, rate-limited): `POST /checkout`
   → create a **Checkout Session** (`mode=subscription`, tier→server `price_id`,
   `customer_id` bound to the authed user) and return its URL for redirect; `POST
   /portal` → Customer Portal session (manage/cancel/card). **No card data touches us
   (§0.A); customer bound server-side (§0.C); prices server-defined (§0.B).**
3. **Stripe webhook** (`/webhooks/stripe`, root-mounted): **signature-verified
   (mandatory)**, idempotent (event-id dedup table), driving the §4 transitions
   through the **existing** `UserService.upgrade_tier`/`apply_signup_bonus` + the
   monthly-credit grant. **This is the sole entitlement-granting path (§0.B).**
4. **Attach the gates:** `require_feature("live_draft")` on `/draft/start`;
   `require_credits(...)` on trade/waiver routes as they're built.
5. **Frontend (UX only):** Pricing CTAs → `/billing/checkout` then **redirect to the
   Stripe URL**; Account → `/billing/portal`; global 402/feature-error → upgrade
   prompt; locked-feature affordances. The success-return URL **must not** grant
   anything (§0.B) — it only refreshes `/account/me`.
6. **Tests:** webhook **signature-rejection**; **idempotency** — redelivered
   `event.id` is a no-op AND the same `invoice.id` grants credits once; **grant
   filter** — `subscription_cycle` grants, but `subscription_create` /
   `subscription_update` invoices do **not** (no double-top across a mid-cycle
   update); **downgrade timing** — `subscription.updated{cancel_at_period_end:true}`
   does **not** downgrade, only `subscription.deleted` does, and credits survive it;
   each tier state transition (mocked Stripe events); gate dependencies (allow/deny
   per tier); "client success URL grants nothing" + "client cannot supply
   price/customer" guards; checkout/portal session creation (mocked SDK). Plus the §4
   local Stripe-CLI flow.
7. **Cleanup (separate PR):** remove the stale `stage-25` tier block (Decision #2).

---

## Appendix — files cited

- Tiers/credits (source of truth): `backend/models/user.py`
- Tier logic / gates: `backend/services/feature_service.py`,
  `backend/services/user_service.py`, `backend/services/credit_service.py`
- Gate dependencies: `backend/core/dependencies.py:281-331`; auth path `:99-232`
- Webhook template + mount: `backend/routers/webhooks.py`, `backend/main.py:89-94`
- Account/entitlement read API: `backend/routers/account.py`
- Config / secrets: `backend/config.py`, `.env.example`
- Frontend surfaces: `frontend/src/pages/Pricing.jsx`,
  `frontend/src/pages/Account.jsx`, `frontend/src/components/landing/PricingTable.jsx`
- Stale tier doc: `docs/stages/stage-25-saas-foundation.md:251-322`
- Prose pricing: `CLAUDE.md` → "SaaS Pricing (Stages 25-30)"
