# Stage 26: User Authentication & Accounts

## Before starting, read:
- `docs/stages/stage-25-saas-foundation.md` (must be complete)
- `docs/APP_DESIGN.md`
- `docs/rules/PATTERNS.md`

---

## Goal
User sign-up, login, session management, and account dashboard.
Users can create accounts, manage their subscription tier, view
credit usage, and connect their fantasy platforms.

---

## Auth provider: Clerk

Use Clerk for authentication. It handles:
- Email/password signup
- Google OAuth (most users will prefer this)
- Session tokens (JWT)
- Password reset flows
- Email verification

Clerk is free up to 10,000 monthly active users — sufficient for launch.

```bash
# Install
pip install clerk-backend-api --break-system-packages
npm install @clerk/clerk-react
```

Required environment variables:
```
CLERK_SECRET_KEY=sk_...
CLERK_PUBLISHABLE_KEY=pk_...
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_...
```

---

## Backend: FastAPI auth middleware

### JWT verification

```python
# backend/middleware/auth.py

from clerk_backend_api import Clerk
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer

clerk = Clerk(bearer_auth=CLERK_SECRET_KEY)
security = HTTPBearer()

async def get_current_user_id(
    request: Request,
    token: str = Depends(security),
) -> str:
    """
    Verify Clerk JWT and return user's external_id.
    Used as a FastAPI dependency on all protected routes.
    """
    try:
        claims = clerk.verify_token(token.credentials)
        return claims["sub"]  # Clerk user ID
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token"
        )

async def get_current_user(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Get or create the User record in our DB.
    Called on every authenticated request.
    """
    result = await db.execute(
        select(User).where(User.external_id == user_id)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        # First time this user has hit our API
        # Create their record
        user = User(
            external_id=user_id,
            email="",  # populated from Clerk webhook
            tier="intro",
            credits_remaining=0,
            # Signup bonus applied after tier confirmed
            # via Stripe webhook on first payment
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    
    return user
```

### Clerk webhook for user events

```python
# backend/routers/webhooks.py

@router.post("/webhooks/clerk")
async def clerk_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle Clerk user lifecycle events.
    Svix signature verification required.
    """
    payload = await request.body()
    headers = dict(request.headers)
    
    # Verify webhook signature
    wh = Webhook(CLERK_WEBHOOK_SECRET)
    try:
        event = wh.verify(payload, headers)
    except WebhookVerificationError:
        raise HTTPException(status_code=400)
    
    event_type = event["type"]
    data = event["data"]
    
    if event_type == "user.created":
        # User signed up — ensure DB record exists
        # Tier and credits set after payment confirmed
        # via Stripe webhook
        await db.execute(
            insert(User)
            .values(
                external_id=data["id"],
                email=data["email_addresses"][0]["email_address"],
                display_name=(
                    f"{data.get('first_name', '')} "
                    f"{data.get('last_name', '')}"
                ).strip(),
                tier="intro",
                credits_remaining=0,
            )
            .on_conflict_do_nothing()
        )
    
    elif event_type == "customer.subscription.created":
        # Stripe confirms payment — apply signup bonus
        tier = data.get("tier")  # passed via Stripe metadata
        bonus = TIER_LIMITS.get(tier, {}).get(
            "credits_signup_bonus", 0
        )
        await db.execute(
            update(User)
            .where(User.external_id == data["user_id"])
            .values(
                tier=tier,
                credits_remaining=bonus,
            )
        )
    
    elif event_type == "user.deleted":
        # Soft delete — keep data for legal/billing
        await db.execute(
            update(User)
            .where(User.external_id == data["id"])
            .values(deleted_at=datetime.utcnow())
        )
    
    await db.commit()
    return {"ok": True}
```

---

## Backend: Account endpoints

```python
# backend/routers/account.py

GET  /account/me
     → Returns user profile, tier, credits, leagues

GET  /account/credits
     → Returns credit balance, usage history,
       monthly limit, reset date

GET  /account/usage
     → Returns credit usage log (last 30 days)

POST /account/leagues
     → Add a new league (creates user_leagues record)
     → Body: {platform, league_id, team_count,
              draft_type, scoring, budget}

GET  /account/leagues
     → List all user's leagues

DELETE /account/leagues/{league_id}
     → Remove a league

PUT /account/leagues/{league_id}
     → Update league settings
```

### Monthly credit reset

APScheduler job runs on the 1st of each month.
Only Standard and Pro users get monthly credits.
Intro users have no monthly reset — signup bonus only.

```python
scheduler.add_job(
    reset_monthly_credits,
    'cron',
    day=1,
    hour=0,
    id='monthly_credit_reset'
)

async def reset_monthly_credits():
    """
    Add monthly credits for Standard and Pro users.
    Does NOT reset to limit — ADDS to current balance.
    Unused credits carry over (encourages saving for draft).
    Intro users: no monthly credits, skip entirely.
    """
    for tier in ("standard", "pro"):
        monthly = TIER_LIMITS[tier]["credits_monthly"]
        await db.execute(
            update(User)
            .where(User.tier == tier)
            .values(
                credits_remaining=User.credits_remaining + monthly
            )
        )
```

Note: credits ADD to balance, they don't reset to a cap.
A Standard user who doesn't use their 20cr in October
will have 40cr in November. This rewards loyal subscribers
and reduces the "use it or lose it" pressure that
frustrates users.

---

## Frontend: Auth integration

### Install and configure Clerk

```javascript
// frontend/src/main.jsx
import { ClerkProvider } from '@clerk/clerk-react'

const PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY

root.render(
  <ClerkProvider publishableKey={PUBLISHABLE_KEY}>
    <App />
  </ClerkProvider>
)
```

### Protected routes

```javascript
// frontend/src/App.jsx
import { SignedIn, SignedOut, RedirectToSignIn } from '@clerk/clerk-react'

function ProtectedRoute({ children }) {
  return (
    <>
      <SignedIn>{children}</SignedIn>
      <SignedOut><RedirectToSignIn /></SignedOut>
    </>
  )
}

// Routes
<Route path="/dashboard" element={
  <ProtectedRoute><Dashboard /></ProtectedRoute>
} />
<Route path="/sign-in" element={<SignInPage />} />
<Route path="/sign-up" element={<SignUpPage />} />
```

### Auth token in API calls

```javascript
// frontend/src/lib/api.js
import { useAuth } from '@clerk/clerk-react'

export function useApi() {
  const { getToken } = useAuth()
  
  const apiCall = async (endpoint, options = {}) => {
    const token = await getToken()
    return fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
        ...options.headers,
      }
    })
  }
  
  return { apiCall }
}
```

---

## Frontend: Account dashboard page

`frontend/src/pages/Account.jsx`

```
┌─────────────────────────────────────────────────────┐
│ My Account                                          │
├─────────────────────────────────────────────────────┤
│                                                     │
│ Plan: Standard ($9/mo)           [Upgrade to Pro →] │
│                                                     │
│ CREDITS                                             │
│ ████████░░░░░░░░░░  42 credits remaining            │
│ +20 credits added Nov 1  · [Buy more credits]       │
│                                                     │
│ MY LEAGUES                                          │
│ ┌─────────────────────────────────────────────┐    │
│ │ 🏈 The League (Yahoo)                       │    │
│ │ 12-team PPR Auction · 2026                  │    │
│ │ Draft: Aug 23 · Live draft: READY ✓         │    │
│ │ Last synced: 2 hours ago   [Sync] [Settings]│    │
│ └─────────────────────────────────────────────┘    │
│ [+ Add League] (1 of 2 used)                       │
│                                                     │
│ RECENT CREDIT USAGE                                 │
│ Trade analysis        Oct 12  -10 credits           │
│ Waiver wire run       Oct 9   -8 credits            │
│ Trade analysis        Oct 5   -10 credits           │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

## Subscription tiers

Implement in `backend/models/user.py`:

```python
TIER_LIMITS = {
    # NOTE: No "free" tier. Intro at $5 is the entry point.
    # This anchors Standard at $9 as an obvious upgrade.

    "intro": {
        # $5/month or $15/season
        # One-time signup bonus: 25 credits (no monthly reset)
        "credits_monthly": 0,
        "credits_signup_bonus": 25,
        "max_leagues": 1,
        "live_draft": False,       # not unlocked
        "trade_analyzer": False,   # not unlocked
        "trade_finder": False,     # not unlocked
        "waiver_wire": False,      # not unlocked
        "injury_monitoring": True, # free for all tiers
        # Features: projections, draft board, news feed,
        # league sync + history, manager tendencies,
        # personalized draft board, injury monitoring
    },

    "standard": {
        # $9/month or $29/season
        # One-time signup bonus: 75 credits
        # Monthly credits: 20/month (enough for ~2 trades)
        "credits_monthly": 20,
        "credits_signup_bonus": 75,
        "max_leagues": 2,
        "live_draft": True,        # 1 per synced league
        "trade_analyzer": True,    # 10cr per trade
        "trade_finder": False,     # Pro only
        "waiver_wire": True,       # 8cr per week
        "injury_monitoring": True,
        # Live draft included for up to 2 leagues.
        # Heavy in-season users will buy extra credits.
    },

    "pro": {
        # $18/month or $49/season
        # One-time signup bonus: 200 credits
        # Monthly credits: 50/month
        "credits_monthly": 50,
        "credits_signup_bonus": 200,
        "max_leagues": None,       # unlimited
        "live_draft": True,        # 1 per synced league, unlimited leagues
        "trade_analyzer": True,    # 10cr per trade
        "trade_finder": True,      # 20cr per run
        "waiver_wire": True,       # 8cr per week
        "injury_monitoring": True,
        # Full tool. Multi-league players and power users.
    },
}

# Credit costs per action — applied after feature access check
# Feature access (tier) is checked first.
# Credits are only deducted if feature is unlocked for the tier.
CREDIT_COSTS = {
    "trade_analysis":    10,  # ~$0.15 AI cost
    "trade_finder":      20,  # ~$0.50 AI cost (Pro only)
    "waiver_wire":        8,  # ~$0.05 AI cost

    # Live draft is a tier entitlement — NOT a credit cost.
    # Included for Standard (up to 2 leagues) and Pro (unlimited).
    # No credits deducted for live draft usage.

    # These are always free — no credits, no tier gate:
    # projections, draft board, news, player profiles,
    # injury monitoring, league sync, draft history
}

# Credit purchase packs
CREDIT_PACKS = {
    "small":  {"price_usd": 5,  "credits": 75},
    "medium": {"price_usd": 10, "credits": 175},
    "large":  {"price_usd": 25, "credits": 500},
}

def check_feature_access(user: User, feature: str) -> bool:
    """
    Check if user's tier unlocks this feature.
    Credits are irrelevant here — this is access only.
    """
    limits = TIER_LIMITS.get(user.tier, {})
    return limits.get(feature, False)

def get_credit_cost(action: str) -> int:
    """Returns credit cost for an action, 0 if free."""
    return CREDIT_COSTS.get(action, 0)

def check_feature_access(user: User, feature: str) -> bool:
    limits = TIER_LIMITS.get(user.tier, TIER_LIMITS["free"])
    return limits.get(feature, False)
```

Gate features in API routes:
```python
@router.post("/draft/connect")
async def connect_live_draft(
    user: User = Depends(get_current_user),
):
    if not check_feature_access(user, "live_draft"):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "feature_not_available",
                "required_tier": "pro",
                "upgrade_url": "/pricing",
            }
        )
```

---

## Stripe integration (payment)

```bash
pip install stripe --break-system-packages
```

```python
# backend/routers/billing.py

POST /billing/create-checkout
     → Creates Stripe checkout session for upgrade
     → Returns {checkout_url}

POST /billing/portal
     → Creates Stripe customer portal URL
     → For managing subscription, payment method

POST /webhooks/stripe
     → Handle subscription events:
       customer.subscription.created → upgrade tier
       customer.subscription.deleted → downgrade tier
       invoice.payment_succeeded → credit top-up

GET /billing/prices
     → Returns current pricing tiers from Stripe
```

**ASK USER for Stripe account and pricing before implementing billing.**

---

## Required test cases

```python
def test_valid_clerk_token_returns_user()
def test_invalid_token_returns_401()
def test_new_user_created_on_first_auth()
def test_credits_deducted_on_action()
def test_insufficient_credits_returns_402()
def test_feature_gated_for_free_tier()
def test_feature_accessible_for_pro_tier()
def test_monthly_credit_reset_fires()
def test_user_cannot_access_other_users_leagues()
def test_clerk_webhook_creates_user_record()
def test_clerk_webhook_verifies_signature()
```

---

## Verification before marking complete

1. Sign up with email → user record created in DB
2. Sign in with Google → works, same user record
3. Protected routes redirect to sign-in when unauthenticated
4. API returns 401 without token, 200 with valid token
5. Credits deduct on pipeline run
6. Free tier user blocked from live_draft feature
7. Pro tier user can access live_draft
8. **ASK USER** to test sign-up flow end to end

---

## Commit
```
feat(saas): user authentication and accounts

Clerk auth integration — email + Google OAuth.
JWT verification middleware on all protected routes.
User model with tier, credits, monthly limits.
Account dashboard: credits, leagues, usage history.
Feature gating by tier (free/starter/pro/league).
Monthly credit reset APScheduler job.
Stripe billing endpoints (checkout, portal, webhooks).
Clerk webhook syncs user lifecycle events.
Coverage: X%.
```
