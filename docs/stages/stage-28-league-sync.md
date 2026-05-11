# Stage 28: League Sync — Yahoo, ESPN, Sleeper

## Before starting, read:
- `docs/stages/stage-25-saas-foundation.md` (must be complete)
- `docs/stages/stage-26-user-auth.md` (must be complete)
- `docs/LIVE_DRAFT.md`
- `docs/AGENTS.md`

---

## Goal
Users can connect their fantasy league from Yahoo, ESPN, or Sleeper.
The system imports league settings (team count, scoring, roster, budget),
historical draft results, and manager roster data.

Yahoo OAuth is already built for the single-user version.
This stage adapts it for multi-user and adds ESPN and Sleeper.

---

## Platform overview

| Platform | Auth method | API quality | Notes |
|----------|------------|-------------|-------|
| Yahoo | OAuth 2.0 | Official, well-documented | Already built in Stage 10 |
| Sleeper | No auth (public API) | Excellent, stable | Easiest to integrate |
| ESPN | Cookie-based (unofficial) | Fragile but widely used | Most common casual league |

---

## Part 1 — Yahoo (adapt existing for multi-user)

### Problem with current implementation
The current Yahoo OAuth stores a single `YAHOO_REFRESH_TOKEN` in `.env`.
For SaaS, each user needs their own OAuth token stored securely per user.

### Changes needed

**Store tokens per user, not in .env:**
```python
# backend/models/platform_credential.py

class PlatformCredential(Base):
    __tablename__ = "platform_credentials"
    
    id          = Column(UUID, primary_key=True)
    user_id     = Column(UUID, ForeignKey("users.id"))
    platform    = Column(String(20))   # yahoo | espn | sleeper
    access_token  = Column(Text)       # encrypted at rest
    refresh_token = Column(Text)       # encrypted at rest
    token_expires = Column(DateTime)
    league_id   = Column(String(100))
    created_at  = Column(DateTime)
    updated_at  = Column(DateTime)
```

**Encrypt tokens at rest:**
```python
from cryptography.fernet import Fernet

# PLATFORM_TOKEN_ENCRYPTION_KEY in environment
fernet = Fernet(PLATFORM_TOKEN_ENCRYPTION_KEY)

def encrypt_token(token: str) -> str:
    return fernet.encrypt(token.encode()).decode()

def decrypt_token(encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()
```

**Per-user OAuth flow:**
```python
# backend/routers/auth_yahoo.py

GET /auth/yahoo/connect
    → Redirects user to Yahoo OAuth
    → State param encodes user_id for callback

GET /auth/yahoo/callback
    → Exchanges code for tokens
    → Stores encrypted tokens in platform_credentials
    → for THIS user's user_id
    → Redirects to /account with success message

POST /auth/yahoo/disconnect
    → Deletes platform_credentials for this user
    → Removes associated league data
```

**Update YahooAPI to accept per-user tokens:**
```python
class YahooAPI:
    def __init__(self, user_id: str, db: AsyncSession):
        self.user_id = user_id
        self.db = db
        self._credentials = None
    
    async def _get_credentials(self) -> PlatformCredential:
        if self._credentials:
            return self._credentials
        result = await self.db.execute(
            select(PlatformCredential)
            .where(
                PlatformCredential.user_id == self.user_id,
                PlatformCredential.platform == "yahoo"
            )
        )
        creds = result.scalar_one_or_none()
        if not creds:
            raise HTTPException(
                status_code=403,
                detail="Yahoo not connected"
            )
        self._credentials = creds
        return creds
    
    async def _get_access_token(self) -> str:
        creds = await self._get_credentials()
        # Refresh if expired
        if creds.token_expires < datetime.utcnow():
            await self._refresh_token(creds)
        return decrypt_token(creds.access_token)
```

---

## Part 2 — Sleeper (new integration)

Sleeper has an excellent public REST API. No OAuth required.
Users just provide their username and league ID.

```python
# backend/integrations/sleeper_api.py

SLEEPER_BASE = "https://api.sleeper.app/v1"

class SleeperAPI:
    """
    Sleeper public API — no auth required.
    Rate limit: 1000 requests/minute (generous).
    """
    
    async def get_user(self, username: str) -> dict:
        """Get user info by username"""
        return await self._get(f"/user/{username}")
    
    async def get_user_leagues(
        self,
        user_id: str,
        season: int,
        sport: str = "nfl",
    ) -> list[dict]:
        """Get all leagues for a user in a season"""
        return await self._get(
            f"/user/{user_id}/leagues/{sport}/{season}"
        )
    
    async def get_league(self, league_id: str) -> dict:
        """Get league settings and metadata"""
        return await self._get(f"/league/{league_id}")
    
    async def get_rosters(self, league_id: str) -> list[dict]:
        """Get all rosters (teams) in a league"""
        return await self._get(f"/league/{league_id}/rosters")
    
    async def get_users_in_league(
        self, league_id: str
    ) -> list[dict]:
        """Get all managers in a league"""
        return await self._get(
            f"/league/{league_id}/users"
        )
    
    async def get_draft(self, draft_id: str) -> dict:
        """Get draft settings"""
        return await self._get(f"/draft/{draft_id}")
    
    async def get_draft_picks(
        self, draft_id: str
    ) -> list[dict]:
        """Get all picks from a draft"""
        return await self._get(f"/draft/{draft_id}/picks")
    
    async def get_league_drafts(
        self, league_id: str
    ) -> list[dict]:
        """Get all drafts for a league"""
        return await self._get(
            f"/league/{league_id}/drafts"
        )
    
    async def parse_league_config(
        self, league_id: str
    ) -> LeagueConfig:
        """
        Convert Sleeper league settings to LeagueConfig.
        
        Sleeper draft types: auction | snake | linear
        Sleeper scoring: standard | half_ppr | ppr
        """
        league = await self.get_league(league_id)
        settings = league.get("settings", {})
        scoring_settings = league.get("scoring_settings", {})
        
        # Determine scoring format
        rec_points = scoring_settings.get("rec", 0)
        if rec_points >= 1.0:
            scoring = "ppr"
        elif rec_points >= 0.5:
            scoring = "half_ppr"
        else:
            scoring = "standard"
        
        # Draft type
        draft_type = "auction" if settings.get(
            "draft_pick_trading"
        ) else "snake"
        # Better check: look at actual draft
        drafts = await self.get_league_drafts(league_id)
        if drafts:
            draft_type = drafts[0].get("type", "snake")
            if draft_type not in ("auction", "snake"):
                draft_type = "snake"
        
        return LeagueConfig(
            team_count=league.get("total_rosters", 12),
            draft_type=draft_type,
            scoring=scoring,
            budget=settings.get("auction_budget", 200),
            platform="sleeper",
            league_id=league_id,
        )
    
    async def get_historical_drafts(
        self,
        league_id: str,
    ) -> list[dict]:
        """
        Get all historical draft picks with player
        names and auction prices (if auction format).
        """
        drafts = await self.get_league_drafts(league_id)
        all_picks = []
        
        for draft in drafts:
            picks = await self.get_draft_picks(draft["draft_id"])
            for pick in picks:
                player_id = pick.get("player_id")
                metadata = pick.get("metadata", {})
                all_picks.append({
                    "player_name": metadata.get("first_name", "") + 
                                   " " + metadata.get("last_name", ""),
                    "position": metadata.get("position"),
                    "team_abbr": metadata.get("team"),
                    "auction_price": pick.get("amount"),
                    # None for snake drafts
                    "pick_number": pick.get("pick_no"),
                    "round": pick.get("round"),
                    "manager_name": None,
                    # Needs user lookup
                    "season": draft.get("season"),
                    "draft_type": draft.get("type"),
                })
        
        return all_picks
    
    async def _get(self, endpoint: str) -> dict | list:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{SLEEPER_BASE}{endpoint}"
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.json()
```

---

## Part 3 — ESPN (unofficial API)

ESPN uses cookie-based auth. The user provides their
`espn_s2` and `SWID` cookies from their browser.

```python
# backend/integrations/espn_api.py

ESPN_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

class ESPNAPI:
    """
    ESPN Fantasy Football unofficial API.
    Requires espn_s2 and SWID cookies for private leagues.
    Public leagues work without authentication.
    
    Warning: This API is unofficial and may change.
    ESPN has broken this periodically with app updates.
    """
    
    def __init__(
        self,
        espn_s2: str,
        swid: str,
        league_id: str,
        season: int,
    ):
        self.cookies = {"espn_s2": espn_s2, "SWID": swid}
        self.league_id = league_id
        self.season = season
        self.base = (
            f"{ESPN_BASE}/seasons/{season}"
            f"/segments/0/leagues/{league_id}"
        )
    
    async def get_league_info(self) -> dict:
        return await self._get("", params={
            "view": "mSettings"
        })
    
    async def get_draft(self) -> dict:
        return await self._get("", params={
            "view": "mDraftDetail"
        })
    
    async def get_teams(self) -> list[dict]:
        data = await self._get("", params={
            "view": "mTeam"
        })
        return data.get("teams", [])
    
    async def get_members(self) -> list[dict]:
        data = await self._get("", params={
            "view": "mTeam"
        })
        return data.get("members", [])
    
    async def parse_league_config(self) -> LeagueConfig:
        """Convert ESPN league settings to LeagueConfig"""
        data = await self.get_league_info()
        settings = data.get("settings", {})
        draft_settings = settings.get("draftSettings", {})
        scoring_settings = settings.get("scoringSettings", {})
        
        # Scoring
        scoring_items = {
            item["statId"]: item["pointsOverride"]
            for item in scoring_settings.get("scoringItems", [])
        }
        # ESPN stat ID 53 = reception
        rec_points = scoring_items.get(53, 0)
        if rec_points >= 1.0:
            scoring = "ppr"
        elif rec_points >= 0.5:
            scoring = "half_ppr"
        else:
            scoring = "standard"
        
        draft_type = draft_settings.get("type", "SNAKE")
        if draft_type == "AUCTION":
            draft_type = "auction"
        else:
            draft_type = "snake"
        
        return LeagueConfig(
            team_count=settings.get("size", 12),
            draft_type=draft_type,
            scoring=scoring,
            budget=draft_settings.get("auctionBudget", 200),
            platform="espn",
            league_id=self.league_id,
            season_year=self.season,
        )
    
    async def get_historical_draft_picks(self) -> list[dict]:
        """Get all draft picks with prices"""
        data = await self.get_draft()
        detail = data.get("draftDetail", {})
        picks_raw = detail.get("picks", [])
        
        # ESPN player lookup needed for names
        player_map = await self._get_player_map()
        
        picks = []
        for pick in picks_raw:
            player_id = pick.get("playerId")
            player_info = player_map.get(player_id, {})
            picks.append({
                "player_name": player_info.get("fullName", ""),
                "position": player_info.get("defaultPositionId"),
                "auction_price": pick.get("bidAmount"),
                "pick_number": pick.get("overallPickNumber"),
                "round": pick.get("roundId"),
                "team_id": pick.get("teamId"),
                "season": self.season,
            })
        
        return picks
    
    async def _get(
        self, endpoint: str, params: dict = None
    ) -> dict:
        url = f"{self.base}{endpoint}"
        async with aiohttp.ClientSession(
            cookies=self.cookies
        ) as session:
            async with session.get(
                url, params=params
            ) as resp:
                if resp.status == 401:
                    raise HTTPException(
                        status_code=401,
                        detail="ESPN authentication failed. "
                               "Check your espn_s2 and SWID cookies."
                    )
                resp.raise_for_status()
                return await resp.json()
```

**How users get their ESPN cookies:**
```
To connect your ESPN league:
1. Log in to ESPN Fantasy on your browser
2. Open Developer Tools (F12)
3. Go to Application → Cookies → espn.com
4. Copy the values for:
   - espn_s2 (long alphanumeric string)
   - SWID (format: {XXXXXXXX-XXXX-...})
5. Paste both into the connection form
```

Show this as a step-by-step guide in the UI with screenshots.

---

## Part 4 — League sync service

```python
# backend/services/league_sync.py

class LeagueSyncService:
    """
    Unified sync service for all platforms.
    Imports league settings and historical draft data
    into user_leagues and league_auction_history tables.
    """
    
    def __init__(self, user_id: str, db: AsyncSession):
        self.user_id = user_id
        self.db = db
    
    async def sync_yahoo_league(
        self,
        league_id: str,
        credentials: PlatformCredential,
    ) -> UserLeague:
        api = YahooAPI(user_id=self.user_id, db=self.db)
        config = await api.parse_league_config(league_id)
        league = await self._upsert_league(config, "yahoo")
        await self._sync_draft_history_yahoo(api, league_id)
        return league
    
    async def sync_sleeper_league(
        self,
        league_id: str,
    ) -> UserLeague:
        api = SleeperAPI()
        config = await api.parse_league_config(league_id)
        league = await self._upsert_league(config, "sleeper")
        picks = await api.get_historical_drafts(league_id)
        await self._store_picks(picks, league.id)
        return league
    
    async def sync_espn_league(
        self,
        league_id: str,
        espn_s2: str,
        swid: str,
        season: int,
    ) -> UserLeague:
        api = ESPNAPI(espn_s2, swid, league_id, season)
        config = await api.parse_league_config()
        league = await self._upsert_league(config, "espn")
        picks = await api.get_historical_draft_picks()
        await self._store_picks(picks, league.id)
        return league
    
    async def _upsert_league(
        self,
        config: LeagueConfig,
        platform: str,
    ) -> UserLeague:
        """Create or update user_leagues record"""
        await self.db.execute(
            insert(UserLeague)
            .values(
                user_id=self.user_id,
                platform=platform,
                league_id=config.league_id,
                team_count=config.team_count,
                draft_type=config.draft_type,
                scoring=config.scoring,
                budget=config.budget,
                season_year=config.season_year,
                last_synced=datetime.utcnow(),
            )
            .on_conflict_do_update(
                index_elements=[
                    "user_id", "platform",
                    "league_id", "season_year"
                ],
                set_={
                    "team_count": config.team_count,
                    "draft_type": config.draft_type,
                    "scoring": config.scoring,
                    "budget": config.budget,
                    "last_synced": datetime.utcnow(),
                }
            )
        )
        await self.db.commit()
    
    async def _store_picks(
        self,
        picks: list[dict],
        user_league_id: str,
    ) -> None:
        """Store historical draft picks for this user's league"""
        for pick in picks:
            if not pick.get("player_name"):
                continue
            await self.db.execute(
                insert(LeagueAuctionHistory)
                .values(
                    user_id=self.user_id,
                    user_league_id=user_league_id,
                    player_name=pick["player_name"],
                    position=pick.get("position"),
                    auction_price=pick.get("auction_price"),
                    pick_number=pick.get("pick_number"),
                    manager_name=pick.get("manager_name"),
                    season=pick.get("season"),
                )
                .on_conflict_do_nothing()
            )
        await self.db.commit()
```

---

## Part 5 — League setup UI

`frontend/src/pages/LeagueSetup.jsx`

Multi-step wizard for connecting a league:

```
Step 1: Choose Platform
  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │  Yahoo   │  │  ESPN    │  │  Sleeper │
  │ Fantasy  │  │ Fantasy  │  │          │
  └──────────┘  └──────────┘  └──────────┘

Step 2: Connect (platform-specific)
  Yahoo:   [Connect with Yahoo →] (OAuth button)
  ESPN:    Enter espn_s2 cookie + SWID cookie
           with step-by-step guide
  Sleeper: Enter your Sleeper username

Step 3: Select League
  (After auth, show list of their leagues)
  ┌─────────────────────────────────────────┐
  │ ○ The League (12-team PPR Auction)      │
  │ ○ Keeper League (10-team Half PPR Snake)│
  └─────────────────────────────────────────┘

Step 4: Confirm Settings
  Team count: 12
  Format: PPR Auction
  Budget: $200
  
  [These look right — Import League →]
  [Edit Settings]

Step 5: Importing...
  ✓ League settings imported
  ✓ 4 years of draft history imported (720 picks)
  ✓ Manager profiles built
  [Go to Dashboard →]
```

---

## Part 6 — API endpoints

```python
# backend/routers/leagues.py

POST /leagues/connect/yahoo
     → Body: {league_id}
     → Requires Yahoo to be connected (OAuth)

POST /leagues/connect/sleeper  
     → Body: {username, league_id}
     → No auth needed

POST /leagues/connect/espn
     → Body: {league_id, espn_s2, swid, season}
     → ESPn cookies

POST /leagues/{id}/sync
     → Re-sync league data
     → Deducts credits: 0 (sync is free)

GET  /leagues/{id}/status
     → Returns sync status, last_synced, pick count

DELETE /leagues/{id}
     → Remove league and all associated data
```

---

## Required test cases

```python
# Sleeper
def test_sleeper_get_league_returns_settings()
def test_sleeper_parse_ppr_scoring()
def test_sleeper_parse_half_ppr_scoring()
def test_sleeper_parse_auction_draft()
def test_sleeper_parse_snake_draft()
def test_sleeper_historical_draft_picks_returned()

# ESPN
def test_espn_parse_league_settings()
def test_espn_auction_budget_extracted()
def test_espn_invalid_cookies_returns_401()

# Yahoo multi-user
def test_yahoo_tokens_stored_per_user()
def test_yahoo_token_refresh_on_expiry()
def test_user_a_cannot_access_user_b_yahoo_tokens()

# League sync
def test_league_upserted_not_duplicated()
def test_picks_stored_with_user_id()
def test_sync_deduplicates_picks()
```

---

## Verification before marking complete

1. **Yahoo**: Connect with a real Yahoo account → league imports correctly
2. **Sleeper**: Connect a Sleeper league by username → settings and draft history import
3. **ESPN**: Connect with real cookies → league settings parse correctly
4. Historical draft picks show correct player names, prices, seasons
5. Team count and scoring format correctly detected for all platforms
6. User A's league data not visible to User B
7. **ASK USER** to test their own Yahoo league connection end to end

---

## Commit
```
feat(saas): league sync for Yahoo, ESPN, Sleeper

Yahoo OAuth adapted for multi-user (per-user encrypted tokens).
Sleeper public API integration (no auth required).
ESPN unofficial cookie-based API integration.
LeagueSyncService unifies sync across all platforms.
League setup wizard UI (5-step flow).
Historical draft history imported for all platforms.
LeagueConfig parsed from each platform's API format.
Row-level security: users can only see their own league data.
Coverage: X%.
```
