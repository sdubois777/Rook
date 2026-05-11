# Stage 27: Landing Page & Marketing Site

## Before starting, read:
- `docs/APP_DESIGN.md`
- `docs/stages/stage-25-saas-foundation.md`
- `docs/stages/stage-26-user-auth.md`

**ASK USER before building:**
1. What's the product name?
2. Any color scheme or brand preferences?
3. Do you want a separate marketing domain or same domain as app?
4. Do you have a logo?

---

## Goal
A high-converting landing page that communicates the product's value
proposition, shows real validation data from the 2025 backtest, and
converts visitors into free trial signups.

The landing page must answer three questions immediately:
1. What is this?
2. Why is it better than FantasyPros?
3. How much does it cost?

---

## Tech stack
Same React + Vite + Tailwind as the main app.
Landing page lives at `/` (public route).
App lives at `/dashboard` (protected route).

---

## Page structure

### Hero section

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│    Win Your Fantasy League With AI                          │
│                                                             │
│    The only fantasy tool that reasons about                 │
│    WHY players are undervalued — not just                   │
│    what the consensus says.                                 │
│                                                             │
│    [Start Free →]        [See How It Works]                 │
│                                                             │
│    ✓ 81.5% signal accuracy in 2025 backtesting             │
│    ✓ 98% buy signal accuracy                               │
│    ✓ Works with Yahoo, ESPN, and Sleeper                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Visual: animated draft board or player card from the app.
Dark theme to match the app.

---

### Social proof bar (below hero)

```
┌─────────────────────────────────────────────────────────────┐
│   "JSN was flagged as undervalued at $32.                   │
│    He finished as a top-5 WR with 359 PPR points."         │
│                                                             │
│   "Brian Thomas Jr was flagged as an overpay at $51.       │
│    He finished with just 138 PPR points."                   │
└─────────────────────────────────────────────────────────────┘
```

---

### The Problem section

```
Why do smart people lose fantasy leagues?

Not because they don't know football.
Because every tool they use shows the same consensus
numbers that everyone else sees.

FantasyPros tells you Chase is worth $54.
So does your opponent. So does everyone else.

The edge isn't in knowing who the good players are.
It's in knowing WHY a specific player is undervalued
in YOUR specific league, against YOUR specific opponents.
```

---

### How It Works section

Three steps with icons:

```
1. CONNECT YOUR LEAGUE
   Import from Yahoo, ESPN, or Sleeper.
   The system learns your league's history —
   who your opponents are, what they overpay for,
   what they systematically miss.

2. GET AI-POWERED ANALYSIS
   Six research agents analyze every player:
   injuries, schedule, role changes, target share,
   offensive system, beat reporter signals.
   Not stats. Reasoning.

3. DRAFT WITH CONFIDENCE
   See exactly which players your league is
   mispricing — and why. Bid ceiling, AI assessment,
   and a plain-English auction note for every player.
```

---

### Validation section (most important)

Use real 2025 backtest numbers prominently.

```
┌─────────────────────────────────────────────────────────────┐
│   PROVEN RESULTS — 2025 NFL Season                         │
│                                                             │
│   81.5%    95%      87%      13/15                         │
│   Overall  Buy      Top      Top opportunities              │
│   accuracy signals  accuracy delivered value                │
│                                                             │
│   ─────────────────────────────────────────────────        │
│                                                             │
│   ✅ Jaxon Smith-Njigba                                     │
│      System flagged as BUY at $32                          │
│      Finished as top-5 WR · 359 PPR points                 │
│                                                             │
│   ✅ Chris Olave                                            │
│      System flagged as BUY at $9                           │
│      Delivered 268 PPR points — WR1 production            │
│                                                             │
│   ✅ Brian Thomas Jr.                                       │
│      System flagged as OVERPAY at $51                      │
│      Finished with just 138 PPR — league overpaid $51      │
│                                                             │
│   ✅ Saquon Barkley                                         │
│      System flagged as OVERPAY at $61                      │
│      Finished with just 230 PPR — bust at that price       │
│                                                             │
│   *Based on 2025 NFL season actual results.                 │
│   Backtest uses pre-2025 data to project 2025 outcomes.    │
└─────────────────────────────────────────────────────────────┘
```

---

### Feature comparison section

```
                        This tool   FantasyPros  Underdog
─────────────────────────────────────────────────────────
AI reasoning              ✓            ✗           ✗
League-specific history   ✓            ✗           ✗
Opponent tendency analysis ✓           ✗           ✗
Causal player analysis    ✓            ✗           ✗
Live draft agent          ✓            ✗           ✗
Yahoo/ESPN/Sleeper        ✓            ✓           ✓
Auction support           ✓            ✓           ✓
Snake draft support       ✓            ✓           ✓
Price                   $9-18/mo    $8-10/mo     $15/mo
```

---

### Pricing section

```
┌──────────────┬──────────────┬──────────────┐
│    INTRO     │   STANDARD   │     PRO      │
│   $5/month   │  $9/month    │  $18/month   │
│  $15/season  │  $29/season  │  $49/season  │
├──────────────┼──────────────┼──────────────┤
│ All player   │ Everything   │ Everything   │
│ projections  │ in Intro     │ in Standard  │
│ + draft board│              │              │
│              │ 2 league     │ Unlimited    │
│ 1 league     │ syncs        │ leagues      │
│ sync         │              │              │
│              │ Live draft   │ Live draft   │
│ Injury       │ agent        │ agent        │
│ monitoring   │ (both leagues│ (all leagues)│
│              │              │              │
│ Manager      │ Trade        │ Trade finder │
│ tendencies   │ analyzer     │              │
│              │              │ 50cr/month   │
│ 25cr signup  │ Waiver wire  │              │
│ bonus        │              │ 200cr signup │
│              │ 20cr/month   │ bonus        │
│              │              │              │
│              │ 75cr signup  │              │
│              │ bonus        │              │
│[Get Started] │[Start Trial] │[Start Trial] │
└──────────────┴──────────────┴──────────────┘

Credits explained:
  Browsing projections, draft board,
  news, injury monitoring = FREE always

  Trade analysis:   10 credits per trade
  Waiver wire:       8 credits per week
  Trade finder:     20 credits per run (Pro only)

  Live draft agent: included with Standard + Pro
  — no credits required, use it for every
    league you have synced

Extra credits if you need more:
  $5 = 75cr  ·  $10 = 175cr  ·  $25 = 500cr

All paid plans include a 7-day free trial.
No credit card required to start.
```

---

### FAQ section

Common questions to address:
- "How is this different from FantasyPros Premium?"
- "Does it work for snake drafts?"
- "What leagues/platforms are supported?"
- "How accurate are the projections?"
- "Is my league data private?"
- "What happens after my credits run out?"
- "Can I cancel anytime?"

---

### Footer CTA

```
Ready to stop guessing?

Start your free trial — no credit card required.
50 free credits. Browse every player projection.
Connect your league when you're ready.

[Create Free Account →]
```

---

## Implementation

### Component structure

```
frontend/src/pages/Landing.jsx           — main page
frontend/src/components/landing/
  Hero.jsx                               — hero section
  SocialProof.jsx                        — quote bar
  HowItWorks.jsx                         — 3-step section
  ValidationStats.jsx                    — backtest numbers
  FeatureComparison.jsx                  — comparison table
  PricingTable.jsx                       — pricing cards
  FAQ.jsx                                — accordion FAQ
  LandingNav.jsx                         — nav for landing page
  LandingFooter.jsx                      — footer
```

### Route structure

```javascript
// Landing page uses different layout than app
// No sidebar, different nav
<Route path="/" element={<Landing />} />
<Route path="/sign-in" element={<SignInPage />} />
<Route path="/sign-up" element={<SignUpPage />} />
<Route path="/pricing" element={<PricingPage />} />

// App routes (protected, use app layout with sidebar)
<Route path="/dashboard" element={
  <ProtectedRoute><AppLayout><Dashboard /></AppLayout></ProtectedRoute>
} />
```

### Validation stats component

Pull real backtest numbers from the API:

```javascript
// ValidationStats.jsx
const { data: backtestStats } = useQuery(
  ['backtest', 2025],
  () => api.get('/admin/backtest?season=2025')
)

// Display with animation on scroll into view
<StatCard
  value="81.5%"
  label="Overall accuracy"
  sublabel="2025 season backtest"
/>
```

---

## SEO and meta tags

```html
<title>FantasyManager — AI-Powered Fantasy Football Draft Tool</title>
<meta name="description" content="Win your fantasy league with AI reasoning.
  81.5% signal accuracy in 2025 backtesting. Works with Yahoo, ESPN, Sleeper.
  Auction and snake draft support." />
<meta property="og:title" content="FantasyManager — Win With AI" />
<meta property="og:description" content="The only tool that reasons about
  WHY players are undervalued in your specific league." />
<meta property="og:image" content="/og-image.png" />
```

---

## Analytics

```javascript
// Track key conversion events
// Use Plausible (privacy-friendly, no cookies needed)
npm install plausible-tracker

// Events to track:
plausible('Hero CTA Click')
plausible('Pricing View')
plausible('Sign Up Started')
plausible('Sign Up Completed')
plausible('League Connected')
```

---

## Required test cases

```javascript
test('hero CTA links to sign-up')
test('pricing table shows all four tiers')
test('validation stats render with real numbers')
test('FAQ items expand on click')
test('landing page has correct meta description')
test('sign in link navigates to auth page')
test('mobile layout renders correctly')
```

---

## Verification before marking complete

1. **ASK USER** to review landing page copy — does it represent the product accurately?
2. **ASK USER** to confirm pricing tiers match intended business model
3. Mobile responsive — test at 375px width
4. Validation stats show real 2025 backtest numbers
5. Sign up flow works end to end from landing page
6. Page loads in under 3 seconds
7. No console errors

---

## Commit
```
feat(landing): marketing site and landing page

Hero with value proposition and 2025 accuracy stats.
Validation section with real JSN, Olave, BTJ, Barkley calls.
Pricing table: Free/Starter/Pro/League tiers.
Feature comparison vs FantasyPros and Underdog.
FAQ section addressing common objections.
SEO meta tags and Open Graph.
Plausible analytics for conversion tracking.
Mobile responsive.
Coverage: X%.
```
