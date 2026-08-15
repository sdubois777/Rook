# Rook — Referral Program + Outbound Email: Design

> **Status: decisions LOCKED (August 2026).** Two features land together because
> the referral program is delivered BY email: a signup who never pays is emailed a
> discount code, and a code only spreads if its owner can see it and send it.
> **The percentages are NOT in this document.** They live in
> `backend/models/user.py` (`REFERRAL_PROGRAM`, lines 133-144) and are referenced
> here by key name only — see Decision #2 for why, and for the evidence that the
> alternative has already failed once in this repo.
> Security posture (§0) is a hard requirement: the signature-verified Stripe
> webhook remains the sole grantor of any entitlement or reward, and a
> client-supplied referral code may select a server-side reward definition but may
> never carry a value.

---

## Approved decisions (LOCKED — August 2026)

| # | Decision | Locked outcome and reasoning |
|---|---|---|
| 1 | Three discounts, three audiences | **`welcome_percent_off`** — emailed to a free signup who has not paid, redeemable whenever they subscribe, one per account ever. It is a conversion nudge aimed at someone who already signed up and stalled. **`referred_percent_off`** — a NEW customer who checks out with someone else's referral code, one per account ever. It is set above the welcome rate because it costs us a customer we would not otherwise have had, and because the friend doing the referring needs something worth passing on. **`referrer_percent_off_per_referral`**, stacking to **`referrer_percent_off_cap`** — the reward the referrer earns, one increment per confirmed referral. All four values are keys in `REFERRAL_PROGRAM` (`backend/models/user.py:133-144`); the derived rate comes from `referrer_percent_off()` (`user.py:147-155`). |
| 2 | `backend/models/user.py` is the ONLY source of truth for every percentage | Nothing else — not this document, not a template string, not a React component, not the Stripe seeder — may contain a literal referral percentage. The seeder, the referral service, the public pricing sheet, the email templates and the frontend copy all derive from `REFERRAL_PROGRAM`. This rule exists because the alternative has already failed here: `docs/stripe_billing_design.md` restated the credit costs, pack sizes and tier names, and every one of those restatements is now wrong. See §1 for the line-by-line evidence. A document that restates a number is a document that will disagree with the code, and the reader has no way to tell which is right. |
| 3 | MONTHLY PLANS ONLY | Both discount paths refuse a season checkout. Gate on `interval_is_referral_eligible(interval)` (`user.py:169-171`), which reads `REFERRAL_PROGRAM["eligible_intervals"]`. **Why:** a season pass is a one-time payment (`mode="payment"`, `backend/routers/billing.py:179`) with no recurring invoice. A recurring referrer coupon has nothing to attach to, and "a percentage off the first month" is undefined for a purchase that has no months. **The honest tradeoff:** `price_season_usd / price_monthly_usd` is under four for both paid tiers, and a pass bought in August runs to the March cutoff — roughly six and a half months. The season pass is therefore the cheaper option for anyone who stays past about the fourth month, so in August it is the better deal for most buyers and this decision excludes a large share of checkouts. We are shipping a referral program that most current customers cannot use. **What would have to change to include them:** see §2.4. Flipping `eligible_intervals` to `("monthly", "season")` before that mechanism exists would silently apply a one-time percentage to a full-season price and leave the referrer's reward with nowhere to land. |
| 4 | The referrer reward is ONE coupon at the summed rate | Not N stacked coupons. `referrer_percent_off(count)` computes the total in Python; the webhook attaches the single Stripe coupon matching that rate, replacing whatever coupon is currently on the subscription. The coupon objects are a fixed set — one per value in `referral_percent_tiers()` (`user.py:158-166`). **Why:** (a) Stripe's stacking semantics are ambiguous. Two percent-off discounts on one subscription may compose as additive or as sequential-on-the-remainder, and the answer has changed across API versions. Depending on it means the customer's actual bill is decided by Stripe's arithmetic rather than ours. (b) A per-referral coupon cannot be cleanly reversed. On a refund you would have to remove one specific coupon from a set and trust the re-rating; with one coupon you replace it with the coupon one step down, which is a single idempotent write with an observable before and after. |
| 5 | A refunded referral reverses the redemption; the redeemer's slot stays occupied | On a refund or chargeback tied to a recorded redemption, the row moves to `status='reversed'` with `reversed_at` set (`backend/models/referral.py:110-115`), the referrer's confirmed count drops, and their coupon is re-rated down (to none at zero). **The row is not deleted and the slot is not released.** `UNIQUE(redeemer_user_id, kind)` (`referral.py:79-82`) still binds, so subscribe → take the discount → refund → repeat does not work. This is deliberate asymmetry: the referrer loses the reward they earned from a sale that did not stick, and the redeemer does not get their once-ever discount back. |
| 6 | One redemption per account per kind, ever; self-referral rejected | `UNIQUE(redeemer_user_id, kind)` is the actual guarantee, because it holds under concurrent webhook delivery. The service layer must ALSO check it early, at checkout, so the user is told before they pay rather than discovering at the webhook that the discount they typed a code for did not apply. Self-referral (`referrer_user_id == redeemer_user_id`) is rejected in the service layer rather than as a table CHECK, so it surfaces as a clear 400 at checkout instead of an opaque `IntegrityError` at webhook time (`referral.py:26-31`). |
| 7 | Email provider is Resend, called over `httpx` | No vendor SDK. `httpx` is already a declared dependency (`pyproject.toml:23`) and is imported by 12 non-test modules under `backend/` and `scripts/` (38 references). Adding the `resend` package means a new dependency and a regenerated `uv.lock` in exchange for one POST to one endpoint. The wrapper follows the existing external-service shape: a thin module of module-level functions that reads the API key from `settings` in exactly one place, so tests monkeypatch it with no network — the same pattern as `backend/services/billing/stripe_gateway.py:1-25`. |
| 8 | The welcome email is PROMOTIONAL and is refused without a postal address | `settings.promotional_email_enabled` (`backend/config.py:192-201`) is false while `EMAIL_POSTAL_ADDRESS` is empty, and a promotional send in that state is recorded as skipped rather than sent. Shipping a commercial email with no physical address in the footer is a CAN-SPAM violation, and the failure mode of sending it anyway is worse than the failure mode of not sending it. See §5. |

---

## 0. Security and abuse posture (non-negotiable)

Mirrors §0 of `docs/stripe_billing_design.md`. The dangerous class here is not card
theft — Stripe owns that, and nothing in this feature touches a card. It is a user
granting themselves a discount, or farming discounts across accounts.

### A. The signature-verified webhook is the sole grantor

- **Rewards are granted only in `StripeWebhookService`** (`backend/services/billing/webhook_service.py:138-209`),
  reached only through the signature-verified endpoint (`backend/routers/webhooks.py:117-204`).
  At checkout time nobody has paid, so nothing may be granted there. The checkout
  endpoint's only job is to attach a server-resolved coupon to the session.
- **The success redirect grants nothing.** `success_url` is a plain app URL
  (`backend/routers/billing.py:185`) that any user can hit without paying. It may
  prompt a refresh of `/account/me`; it may never write a redemption row, raise a
  referrer's rate, or set a tier. This is the same rule already stated in
  `docs/stripe_billing_design.md` §0.B.
- **Unverified payloads are rejected with no side effects** — production always
  requires a signature (`webhooks.py:156-170`).

### B. A referral code is UNTRUSTED input

A client-supplied code may **select** a server-side reward definition. It may never
**carry** a value.

- The client posts a code string. The server uppercases it, looks it up in
  `referral_codes`, and derives the percentage from `REFERRAL_PROGRAM`. The client
  never supplies a percentage, a coupon id, or a price.
- This is the identical rule already enforced for prices:
  `docs/stripe_billing_design.md` §0.B ("Prices are server-defined", lines 63-65),
  implemented in `backend/services/billing/catalog.py` — the checkout request accepts
  a tier or pack **name** and nothing else (`backend/routers/billing.py:77-92`).
- `code_redemptions.percent_off` (`referral.py:105-108`) records what was actually
  applied, for audit. It is written by the server from `REFERRAL_PROGRAM`, never read
  from the request.
- **Brute force:** the code space is `len(code_alphabet) ** code_length`, which for the
  configured values is on the order of 10^8 to 10^9 — far beyond guessing at HTTP
  rates, and the billing router already carries a rate-limit dependency
  (`billing.py:38`). The validation endpoint should ride the same limiter. Telling the
  user "that code is not valid" is fine; it is not a useful oracle at that size.

### C. Anti-farming: three independent constraints

| Rule | Where it is guaranteed | Where it is also checked |
|---|---|---|
| Self-referral rejected | Service layer (`referrer_user_id != redeemer_user_id`) | At checkout, so the user gets a 400 before paying |
| One redemption per account per kind, ever | `UNIQUE(redeemer_user_id, kind)` (`referral.py:79-82`; migration `alembic/versions/ref2026email_referral_program_and_email.py:119-121`) | At checkout, for a clear message instead of a post-payment surprise |
| One reward per completed checkout | `UNIQUE(stripe_session_id)` (`referral.py:100-104`; migration lines 116-118) | Insert-or-skip returns whether the row is new; the referrer's rate is raised only on a new row |

The session-id constraint is the same idempotency shape credit packs already use
(`GrantedPackSessionRepository.record_grant`, `backend/repositories/billing_repo.py:60-74`).
It is the stable key because Stripe delivers at least once and a redelivery can carry a
different event id. Above it sits the webhook's global `event.id` dedup
(`webhook_service.py:79-86`), which is a separate layer and not a substitute.

### D. The referral reward email must not identify the referred person

**Precedent:** `backend/routers/feedback.py:8-12` — the block headed "WHAT IT SENDS,
AND WHAT IT DELIBERATELY DOES NOT." A filed issue carries the reporter's opaque
database id and tier and **never** their email address or name, because an outbound
artifact is not a safe place for someone else's address.

The same reasoning applies here, and more strongly, because the subject is a third
party:

- The reward email states the referrer's new confirmed count and new rate. It contains
  **no name, no email address, no partial or masked address, and no league or team
  reference** for the person who redeemed the code.
- The referred person consented to buy a subscription. They did not consent to have
  their purchase disclosed to the person who referred them.
- Timing is the one thing we cannot fully hide: a near-real-time email tells the
  referrer roughly when someone bought. That is accepted, because they already know
  they shared the code and the alternative is not sending at all. Nothing beyond timing
  may leak.
- The same rule governs the account page: it shows a count and a rate, never a list.

### E. Secret hygiene and the unsubscribe link

- `RESEND_API_KEY` is a server-only Railway environment variable. Never in the repo,
  never sent to the client, never logged.
- **The unsubscribe link must not contain a raw email address.** CAN-SPAM requires
  unsubscribe to work without logging in, so the link is necessarily public — which
  means a raw address in the query string lets anyone unsubscribe anyone, and leaks the
  address into server logs and `Referer` headers. Use an opaque, unguessable token
  derived from the address and the app secret. This also satisfies the standing rule
  against putting personal data in URL parameters.

---

## 1. The percentages — source of truth, and the drift this rule prevents

**Canonical, machine-readable:** `backend/models/user.py:133-144` (`REFERRAL_PROGRAM`)
plus the three helpers at `user.py:147-171`. The file's own header comment states the
rule (`user.py:102-131`): "percentages live HERE and nowhere else… A percentage written
into a template string or a React component is the drift this block exists to stop."

This is not a hypothetical risk. `docs/stripe_billing_design.md` restated the numbers
and every restatement is now wrong:

| Restated in `docs/stripe_billing_design.md` | What `backend/models/user.py` actually ships |
|---|---|
| Credit costs `trade_analysis`=10, `trade_finder`=20, `waiver_wire`=8 (lines 113-115, cited as `user.py:64-70`) | `CREDIT_COSTS` at `user.py:86-91` — all three are far lower, and the cited line range now holds `TIER_LIMITS` instead |
| Credit packs $5→75, $10→175, $25→500 (lines 114-116, cited as `user.py:73-77`) | `CREDIT_PACKS` at `user.py:96-100` — different credit counts and a different middle price |
| Tier names `intro`/`standard`/`pro` (lines 17, 19, 193, 250) | `TIER_ORDER` at `user.py:44` — `free`/`standard`/`pro`. There is no `intro` tier |
| League caps "intro 1 / std 2 / pro ∞" (line 250) | `max_leagues` at `user.py:53, 65, 74` — standard is not 2 |

Three of those four restatements even carry a line citation, which made them look
verified. Line numbers move; the numbers themselves moved too. That is the whole
argument for referencing by key name.

**This document therefore names keys, never values.** Where an argument needs a
magnitude — Decision #3's season-versus-monthly tradeoff, §0.B's code space — it is
expressed as a relationship between keys that stays true when the values change.

---

## 2. Discount mechanics in Stripe

### 2.1 The coupon objects

Three shapes, all created by a seeder that derives every rate from `REFERRAL_PROGRAM`:

| Coupon | Rate | Duration | Applies to |
|---|---|---|---|
| welcome | `REFERRAL_PROGRAM["welcome_percent_off"]` | once | The redeemer's first monthly invoice |
| referred | `REFERRAL_PROGRAM["referred_percent_off"]` | once | The redeemer's first monthly invoice |
| referrer (one per rate) | each value from `referral_percent_tiers()` | forever | The referrer's own monthly subscription |

Total objects per Stripe mode: two, plus `len(referral_percent_tiers())`. That count is
bounded by the cap divided by the per-referral increment, so raising the cap adds
coupons and requires a re-seed — worth knowing before changing the config.

Coupons are mode-scoped objects exactly like prices: test-mode ids are useless in live
mode (`docs/stripe_billing_design.md` §6, "Test vs live mode"). The seeder runs once per
mode.

### 2.2 Applying the redeemer's discount

1. The checkout request carries an optional code alongside the tier and interval
   (`backend/routers/billing.py:77-92`).
2. The server rejects it if `interval_is_referral_eligible(interval)` is false
   (Decision #3), if the code does not resolve, if it is the caller's own code, or if
   the caller has already redeemed that kind. All four produce a 400 **before** a
   Checkout Session is created, so the user never pays expecting a discount that will
   not arrive.
3. The server picks the coupon by comparing the two rates in `REFERRAL_PROGRAM` and
   taking the higher — which under the current configuration means a referral code
   beats a held welcome code, as `referral.py:46-47` describes. Comparing values rather
   than hardcoding "referral wins" keeps the behaviour correct if the rates are ever
   reordered.
4. The resolved coupon is attached to the Checkout Session and the code rides in
   session metadata so the webhook can record the redemption against the session id.
   Note that `stripe_gateway.create_checkout_session` (`stripe_gateway.py:44-68`) has no
   discount parameter today — adding one is part of the implementation, not something
   that already exists.

### 2.3 Applying the referrer's reward

On `checkout.session.completed` for an eligible monthly subscription carrying a
referral code:

1. Insert the `code_redemptions` row. If the insert is not new (duplicate session id),
   stop — the reward was already granted.
2. Recount the referrer's `confirmed` redemptions and compute
   `referrer_percent_off(count)`.
3. Set the coupon matching that rate on the referrer's subscription, replacing whatever
   coupon is there (Decision #4).
4. Send the reward email (§0.D governs its contents).

**Stated consequence:** step 3 needs the referrer to hold an active monthly
subscription. A referrer on the free tier or on a season pass earns a rate that is
recorded but not applied until they have a monthly subscription for it to attach to.
The `code_redemptions` rows are the record, so the rate is recoverable — but nothing
notifies the referrer of this, and the account page will show a rate they are not
currently receiving. This should be worded carefully in the UI, and is listed in §7.

### 2.4 What including season passes would require

Decision #3 refuses them. Reversing that is not a config flip:

- **Redeemer side** is mechanically easy — a coupon can discount a one-time Checkout
  line item — but the product question is real: "a percentage off the first month" and
  "a percentage off a whole season" are very different amounts of money to give away,
  and the rate would probably need to differ per interval, which means new
  `REFERRAL_PROGRAM` keys.
- **Referrer side is the actual blocker.** There is no recurring invoice. Including
  season would need either a credit grant instead of a percentage, or a "pending
  discount" record redeemed against the referrer's next purchase — which needs a place
  to store it, a rule for applying it at checkout, and a rule for what happens if the
  referrer never buys again.
- **Reversal gets harder.** A recurring coupon can be re-rated downward at any time. A
  discount already consumed on a completed one-time payment cannot be clawed back, so
  Decision #5 would need a different remedy.

Only after that mechanism exists should `eligible_intervals` (`user.py:138`) change.

---

## 3. Referral lifecycle

```
signup (free)                     -> welcome code emailed (§4)
account page first view           -> referral code generated lazily
friend checks out with the code   -> discount applied at Stripe Checkout
checkout.session.completed        -> redemption row + referrer coupon raised + reward email
refund / chargeback               -> redemption reversed, referrer coupon lowered,
                                     redeemer's slot stays occupied
```

Codes are generated **lazily**, the first time a user looks at their account page,
rather than at signup — so we never mint codes for accounts that never return
(`referral.py:6-9`). `UNIQUE(user_id)` on `referral_codes` means a concurrent
double-generate loses at the database instead of leaving one user holding two live
codes that both attribute rewards.

**Reversal is designed but has no trigger in the shipped dispatch table.**
`StripeWebhookService._DISPATCH` (`webhook_service.py:298-304`) handles five event
types; none of them is a refund or dispute event. Automatic reversal requires adding a
handler for the refund event and resolving it back to a redemption row through the
payment's checkout session. Until that exists, reversal is a manual database operation.
This is recorded honestly in §7 rather than assumed.

---

## 4. Email delivery

### 4.1 Shape

A thin Resend wrapper over `httpx` (Decision #7), mirroring
`backend/services/billing/stripe_gateway.py`: module-level functions, the API key read
from `settings` in exactly one place, no vendor SDK, monkeypatchable in unit tests with
no network. Per `docs/rules/GIT_RULES.md`, the unit suite mocks all external
dependencies — no test may reach Resend or a real database.

### 4.2 The send lock

`email_sends.dedupe_key` is UNIQUE and is claimed **before** the provider call
(`backend/models/email.py:68-73`). Insert-or-skip; if the row is not new, return without
calling Resend. This is the same `pg_insert(...).on_conflict_do_nothing(...)` pattern
the billing repositories use (`backend/repositories/billing_repo.py:22-37`), and it is
what makes a retried webhook or a double-fired background task safe. The key must be a
caller-supplied natural key such as `welcome:<user_id>`; deriving it from a timestamp or
a random value stops it deduplicating anything.

### 4.3 Outcomes

Every attempt gets a row, including the ones that never reach the provider:
`sent` / `failed` / `suppressed` / `skipped` (`email.py:38-41`). A failure keeps its row
so a persistent failure is visible rather than looking like a message nobody tried to
send.

### 4.4 Kill switch and rate cap

- `settings.email_enabled` (`config.py:187-190`) is false when `RESEND_API_KEY` is
  unset. Every send is recorded `skipped` and logged. Nothing crashes, nothing queues.
- `EMAIL_MAX_PER_HOUR` (`config.py:113-117`) caps sends per process per hour. It exists
  because a runaway loop over the users table is what gets a sending domain
  blacklisted, and the domain is much harder to get back than the bug is to fix. It is
  a domain-reputation guard, not a per-recipient throttle.

### 4.5 A failed email never fails the thing that triggered it

A welcome email that cannot be sent must not roll back user creation, and a reward email
that fails must not roll back the coupon change. Wrap the send and log the failure —
the same posture as the best-effort season-cancel block at `webhook_service.py:184-195`,
which explicitly never fails the entitlement grant.

### 4.6 When the welcome email fires

The Clerk `user.created` webhook (`backend/routers/webhooks.py:63-112`) is the only
event that exists without adding a scheduler, and at that moment the user is by
definition unpaid, so the policy condition holds. The send must still re-check that the
user has not paid, because the webhook can be redelivered later.

**The cost of sending at signup:** it also reaches users who would have converted at
full price within the hour. A delayed send — a day or two after signup, only if still
unpaid — targets the intended audience much better. APScheduler is already a dependency
and already runs jobs in this app, so this is feasible; it is simply not in scope here,
and §7 records that no scheduled follow-up exists.

---

## 5. CAN-SPAM and compliance

### 5.1 Which messages are promotional, and why the label matters

| Message | Category | Reasoning |
|---|---|---|
| Welcome discount code | `CATEGORY_PROMOTIONAL` (`email.py:51`) | Its primary purpose is to get the recipient to buy. This is a commercial message by any reading |
| Referral reward notice | `CATEGORY_PROMOTIONAL` | Arguable — it reports a change to the recipient's own billing terms, which reads transactional — but it also exists to encourage more referrals. Classify it promotional. Misclassifying a commercial message as transactional is the violation; misclassifying a transactional message as promotional costs a footer and an unsubscribe link |
| Receipts, payment failures | `CATEGORY_TRANSACTIONAL` (`email.py:50`) | Stripe sends these. Rook sends none today, which `email.py:20-23` already records |

Promotional mail requires a working unsubscribe that is honored promptly, a valid
physical postal address, and non-deceptive subject and From lines. Transactional mail is
exempt from the unsubscribe and postal-address requirements but must still not use
deceptive headers. The rule when unsure is to treat the message as promotional.

### 5.2 Unsubscribe and the suppression list

- Every promotional message carries an unsubscribe link that works **without logging
  in**, using an opaque token rather than a raw address (§0.E).
- Unsubscribing writes to `email_suppressions`, keyed on the lowercased address
  (`email.py:85-94`). The address is the primary key, so a second click is a no-op
  rather than a duplicate row.
- Keying on address rather than `user_id` is deliberate: a bounce or complaint arrives
  from the provider identified only by an address, and an address that complained must
  stay suppressed even if the account is deleted and recreated.
- Every promotional send checks the list first and records `suppressed` (`email.py:40`)
  rather than dropping silently, so a suppressed attempt is a visible row.
- **The migration's `downgrade()` drops this table and the data is not recoverable from
  anywhere else** (`alembic/versions/ref2026email_referral_program_and_email.py:168-177`).
  Re-emailing someone who unsubscribed is a violation, not an annoyance. Export
  `email_suppressions` before downgrading any environment that has ever sent
  promotional mail.

### 5.3 The postal address is a hard gate

`settings.promotional_email_enabled` (`config.py:192-201`) is stricter than
`email_enabled`: promotional sends are refused while `EMAIL_POSTAL_ADDRESS` is empty
(`config.py:108-113`, `.env.example:74-78`). The default is empty, so the safe state is
the default state.

The operator must choose the address. Rook Fantasy Football LLC's address is already
published in both public legal documents
(`docs/business/rook-terms-of-service.md:7-9, 223-225` and
`docs/business/rook-privacy-policy.md:7-8`), so it is the obvious value — but putting an
address into every promotional email is a decision with real-world consequences, and a
mailbox service is the usual alternative. This is an operator choice, not a code default.

### 5.4 OPEN ITEM — the legal documents need a human decision before this ships

**Verified as of this writing: neither `docs/business/rook-terms-of-service.md` nor
`docs/business/rook-privacy-policy.md` mentions referrals, referral codes, discounts
earned for referring, or marketing email.** Specifically:

- **Terms of Service §5** (lines 90-120) covers plans, payment processing, automatic
  renewal, cancellation, price changes, refunds, and the free plan and credits. It says
  nothing about a referral program: who may earn a reward, that a reward changes the
  price the referrer pays, that a reward can be revoked when the referred customer
  refunds (Decision #5), or that discounts are once-per-account and non-transferable.
  A program that changes what a customer is charged, and that we can take back, is a
  term of the agreement.
- **Privacy Policy line 82** states: "We do not share, sell, rent, or transfer your data
  to any third party." That sentence sits under "## 2. Where your data goes", a section
  written about the extension, but the document's opening scope (line 5) covers "the
  Rook browser extension and the Rook web application". Sending mail through Resend
  transfers a recipient's email address to a third-party processor. Whether that
  sentence needs qualifying, and how, is a lawyer's call.
- **Neither document** describes marketing email, how to opt out of it, or how long a
  suppression record is retained.
- **Both files are served live** at `/privacy` and `/terms` from the markdown itself
  (`backend/main.py:405-424`), so any amendment is a content change that deploys with
  the app and needs the "Last updated" dates bumped — currently July 17, 2026 and
  July 13, 2026.

**No legal text is drafted here, deliberately.** This is flagged as requiring a human
decision before the feature reaches real users.

---

## 6. Operations — the runbook to turn this on

Ordered. Steps 1 through 5 are prerequisites; nothing works before them.

1. **Create the Resend account** and add `rookff.com` as a sending domain.
2. **Add the DNS records Resend shows for `rookff.com`:** the SPF record (TXT), the
   DKIM record, and the **MX record Resend requires** for the sending domain. Then
   **wait for Resend to report the domain verified.** Sending from an unverified domain
   fails at the API, and sending without DKIM lands in spam — both already documented at
   `.env.example:66-69`. This step has a waiting period that is outside our control;
   start it first.
3. **Decide the postal address** that will appear in the promotional footer (§5.3).
4. **Set the Railway environment variables** on the production service:
   `RESEND_API_KEY` and `EMAIL_POSTAL_ADDRESS`. **Railway reads no `.env` file** — it
   injects environment variables, so a setting whose only value is a line in
   `.env.example` is `None` in production (`config.py:99-101`). This has already
   happened once in this repo: `GITHUB_ISSUE_REPO` shipped with its value only in
   `.env.example`, so setting the token alone left the feature silently off in
   production with no error anywhere (`config.py:127-135`). `EMAIL_FROM`,
   `EMAIL_REPLY_TO` and `EMAIL_MAX_PER_HOUR` have working defaults
   (`config.py:106-117`) and need setting only to override them.
5. **Run the coupon seeder once per Stripe mode** — once against the test key, once
   against the live key. It creates the welcome coupon, the referred coupon, and one
   coupon per value in `referral_percent_tiers()`, deriving every rate from
   `REFERRAL_PROGRAM`. Coupons are mode-scoped like prices, so test-mode ids are useless
   in live mode. Note that the existing price seeder deliberately refuses a live key
   (`scripts/stripe_seed_test.py:115-121`); the coupon seeder needs either an explicit,
   deliberate live path or a documented manual dashboard procedure — not a silently
   relaxed guard. **Seed before deploying the code that references the coupons**, or the
   first checkout with a code fails.
6. **Confirm the migration is the single head.** Revision `ref2026email`, down revision
   `wvr2026settings`. Railway's start command is
   `alembic upgrade head && uvicorn` (`railway.toml`), so two parallel branches each
   adding a migration produce two heads and the container fails to boot. That is a
   failed deploy, not a degraded one.
7. **Deploy.** Backend changes do nothing in production until released to `main` —
   Railway deploys from `main`, and the reconcile-branch release flow in `CLAUDE.md` is
   the only path from `develop` to `main`. **Stephen drives every release manually**; a
   merged PR on `develop` is not a live feature.
8. **Verify after release**, in this order: send one welcome email to an address you
   control and confirm it arrives with the postal address in the footer; click the
   unsubscribe link **while logged out** and confirm it works and writes an
   `email_suppressions` row; trigger the same send again and confirm it is skipped on
   the dedupe key rather than duplicated; run one test-mode checkout with a referral code
   and confirm a `code_redemptions` row and a raised referrer coupon.

---

## 6A. Hardening applied after adversarial review

Sections 2 and 3 above describe the design as first written. Two review passes found
defects that changed the implementation. Where this section and an earlier section
disagree, **this one is correct** — it was written against the shipped code.

**The welcome code is per-user and unguessable.** It was first a single fixed string,
`ROOK-WELCOME`. That is a discount for anyone who learns the string, including customers
already paying and eventually a coupon-listing site. It is now derived per account by
signing the user id with `SECRET_KEY` and is verified by recomputing it for the account
trying to redeem it, so a forwarded or posted code is worthless to anyone else
(`referral_service.py`, `welcome_code_for`). It is also refused for any account that has
ever held a subscription, not merely one that is on the free tier right now — a customer
who subscribes and cancels reads as free again and would otherwise qualify a second time.

**A discount is reserved before Stripe is called, not recorded after payment.** Opening
the checkout endpoint twice with the same code used to return two Stripe pages both
carrying the coupon, and both could be paid. The endpoint now writes a `pending` row
first, so the uniqueness constraint on (account, discount kind) refuses the second
attempt before any discounted session exists. A reservation older than
`PENDING_TTL_HOURS` (24, matching how long a Stripe Checkout Session stays payable) is
treated as an abandoned checkout and stops blocking, so a customer who closes the Stripe
page is not locked out of a discount they never received.

**Two accounts cannot refer each other.** Rejecting only the case where the referrer and
redeemer are the same row left A and B — both of whom were going to subscribe anyway —
able to redeem each other's codes and each earn a permanent recurring discount, for zero
customers acquired.

**A referrer's rate follows reality, not history.** The count includes only referrals
whose referred account holds a paid tier *right now*, and excludes soft-deleted accounts.
This, not the reversal machinery described in §3, is what actually prevents five throwaway
accounts subscribing once and buying a permanent discount. `STATUS_REVERSED` remains
unreachable in production because no refund or dispute event is handled (see §7).

**A referrer who earns referrals before subscribing now receives them.** The rate is
pushed when a new referral lands and when one goes away, and both need a live subscription
to write to — so everything earned on the free tier was skipped and never revisited. A
user who referred five people and then subscribed started at 0 percent.
`_apply_own_referrer_rate` in `webhook_service.py` is the missing path. This supersedes
the §7 bullet stating that an earned rate is never applied to a free-tier referrer: it is
now applied at the moment they subscribe. It still cannot be applied to a season-pass
holder, which remains true.

**Email sends carry a provider idempotency key.** Making a failed send retryable
introduced a way to send twice: a request that times out is indistinguishable from one
that was rejected, so a message the provider accepted but whose response was lost would
be sent again. Each send now passes its deduplication key to Resend as an
`Idempotency-Key`, so repeating the request returns the original result instead of a
second message. Resend retains those keys for 24 hours; a retry later than that could
still duplicate, which in practice would require a webhook redelivery a day late.

---

## 7. What is NOT built

An honest list. None of these is a bug; each is a deliberate omission with a
consequence.

- **No referral leaderboard**, and no public standing of any kind. The account page
  shows the user's own code, their confirmed count and their current rate. Nothing
  compares users, and nothing lists who redeemed (§0.D forbids the latter).
- **No bounce or complaint webhook from Resend.** Suppression is unsubscribe-only
  today. The consequence is real: a hard-bouncing address is retried by every future
  send, and a spam complaint suppresses nothing. Both reasons already exist as
  constants — `SUPPRESS_BOUNCE` and `SUPPRESS_COMPLAINT` (`email.py:45-46`) — and
  nothing writes them. This is the first thing to add once real volume exists, because
  bounce rate and complaint rate are what a sending domain's reputation is scored on.
- **No automatic reversal trigger.** The dispatch table handles no refund or dispute
  event (`webhook_service.py:298-304`), so Decision #5 is a policy with a manual
  execution path until a handler is added. The columns to record it exist
  (`referral.py:110-115`).
- **No scheduled follow-up** if a welcome code goes unused. One email, then nothing.
  See §4.6 for why the send point is signup and what a delayed send would buy.
- **No admin interface.** Issuing a manual code, reversing a redemption, and clearing a
  suppression are all direct SQL today.
- **No A/B testing of the percentages.** `REFERRAL_PROGRAM` holds one value per
  discount for everyone. Changing a rate is a deploy, and it changes the rate for
  existing referrers too — the coupon a referrer already holds is re-rated only the next
  time their confirmed count changes, so a rate change propagates unevenly across the
  existing base. Anyone changing a percentage should know that.
- **No notification when an earned rate cannot be applied.** A referrer holding a season
  pass banks a rate with no recurring invoice to attach it to, and nothing tells them —
  the account page will show a rate they are not receiving. This no longer applies to a
  free-tier referrer: their banked rate is applied when they subscribe (§6A).
- **No retry job for a failed email.** A send that failed can be re-attempted, but nothing
  schedules one. In practice the only trigger is the originating webhook being redelivered.
  The welcome email fires once from the Clerk signup webhook, so if the mail provider is
  down at that moment and Clerk does not redeliver, that customer never receives it.
- **No alert on sends stuck in the pending state.** A process that dies between claiming
  the send lock and learning the provider's answer leaves a row nothing will ever retry —
  correct, because nobody knows whether the message went out, but such rows accumulate
  with no query or dashboard surfacing them.
- **The hourly send cap is per process.** With more than one web process the effective
  ceiling multiplies, and a capped send writes no database row at all — the application
  log is its only record.
- **No fraud detection beyond the structural constraints.** Nothing detects one person
  creating several accounts with different email addresses and redeeming their own code
  from each. Self-referral is blocked only when the referrer and redeemer are the same
  account row.
- **No localisation.** English only.

---

## Appendix — files cited

- Percentages and helpers (source of truth): `backend/models/user.py:102-171`
- Referral tables: `backend/models/referral.py`
- Email tables: `backend/models/email.py`
- Migration (four tables, revision `ref2026email`):
  `alembic/versions/ref2026email_referral_program_and_email.py`
- Email + Stripe settings: `backend/config.py:76-117, 183-201`; `.env.example:39-81`
- Checkout endpoints: `backend/routers/billing.py`
- Price/tier mapping: `backend/services/billing/catalog.py`
- Stripe wrapper (pattern for the Resend wrapper):
  `backend/services/billing/stripe_gateway.py`
- Webhook state machine: `backend/services/billing/webhook_service.py`
- Webhook endpoints and signature verification: `backend/routers/webhooks.py`
- Insert-or-skip repository pattern: `backend/repositories/billing_repo.py`
- Privacy precedent for outbound artifacts: `backend/routers/feedback.py:8-12`
- Price seeder (pattern and the live-key guard): `scripts/stripe_seed_test.py`
- Public legal documents and how they are served:
  `docs/business/rook-terms-of-service.md`, `docs/business/rook-privacy-policy.md`,
  `backend/main.py:405-424`
- Prior billing design and the drift it demonstrates: `docs/stripe_billing_design.md`
- Deploy command and release rules: `railway.toml`, `CLAUDE.md`
