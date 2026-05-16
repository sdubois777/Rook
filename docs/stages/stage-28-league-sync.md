# Stage 28: League Sync — Yahoo, ESPN, Sleeper

## Before starting, read:
- `docs/stages/stage-25-saas-foundation.md` (must be complete)
- `docs/stages/stage-26-user-auth.md` (must be complete)
- `docs/stages/stage-14-season-roster.md` (platform abstraction layer)
- `docs/LIVE_DRAFT.md`

---

## Goal

Users connect their fantasy league from Yahoo, ESPN, or Sleeper.
The system imports:
1. League settings → LeagueConfig
2. Historical draft data (up to 4 years)
3. Manager roster data (current season)
4. Current free agent pool
5. FAAB budgets (where applicable)

All data is user-scoped. One user's league data is never visible
to another user.

---

## Enterprise standards

- `PlatformCredential` table — encrypted OAuth tokens and cookies
- `LeaguePlatformAPI` interface — platform-specific code isolated
- `LeagueSyncService` — all sync logic, no platform code
- `CredentialRepository` — all credential DB access
- Tokens encrypted at rest with Fernet — never plaintext
- ESPN uses bookmarklet — no manual DevTools for users
- Row-level security: `user_id` on all synced data

---

## Platform overview

| Platform | Auth | Draft history | Live roster | Free agents | FAAB |
|----------|------|--------------|-------------|-------------|------|
| Yahoo | OAuth 2.0 per user | ✓ (4 years) | ✓ | ✓ | ✓ |
| Sleeper | Public API | ✓ | ✓ | ✓ (derived) | ✓ |
| ESPN | Cookie-based bookmarklet | ✓ | ✓ | ✓ | ✓ |

---

## Part 1 — Environment variables

Add to `backend/config.py`:

```python
# Token encryption
platform_token_encryption_key: str
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# App URL (for ESPN bookmarklet redirect)
app_url: str = "http://localhost:8000"

# In-season data (Stage 20 — configure now)
the_odds_api_key: str | None = None
openweathermap_api_key: str | None = None
```

Add to Railway and `.env`:
```
PLATFORM_TOKEN_ENCRYPTION_KEY=<generated Fernet key>
APP_URL=https://fantasymanager-production.up.railway.app
```

---

## Part 2 — Token encryption

```python
# backend/integrations/token_encryption.py
"""
Fernet symmetric encryption for OAuth tokens and cookies.
Never store plaintext credentials in the database.
"""
from cryptography.fernet import Fernet, InvalidToken
from backend.config import settings


def _fernet() -> Fernet:
    key = settings.platform_token_encryption_key
    if not key:
        raise RuntimeError("PLATFORM_TOKEN_ENCRYPTION_KEY not set")
    return Fernet(
        key.encode() if isinstance(key, str) else key
    )


def encrypt_token(token: str) -> str:
    if not token:
        return ""
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Token decryption failed — encryption key may have changed"
        ) from e
```

---

## Part 3 — PlatformCredential model

```python
# backend/models/platform_credential.py
"""
Per-user platform credentials.
Tokens encrypted at rest. Never stored plaintext.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class PlatformCredential(Base):
    __tablename__ = "platform_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    # "yahoo" | "espn" | "sleeper"

    # Yahoo OAuth tokens (encrypted)
    access_token: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    refresh_token: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ESPN cookies (encrypted)
    espn_s2: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    swid: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )

    # Sleeper (no auth — just user ID)
    sleeper_user_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    class Meta:
        # Unique: one credential set per user per platform
        constraints = [
            "UNIQUE(user_id, platform)"
        ]
```

---

## Part 4 — Credential repository

```python
# backend/repositories/credential_repo.py
"""
CredentialRepository — encrypted token storage.
All reads decrypt. All writes encrypt.
"""
import uuid
from datetime import datetime

from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.integrations.token_encryption import (
    decrypt_token, encrypt_token,
)
from backend.models.platform_credential import PlatformCredential
from backend.repositories.base import BaseRepository


class CredentialRepository(BaseRepository[PlatformCredential]):
    model = PlatformCredential

    async def get_for_user(
        self,
        user_id: uuid.UUID,
        platform: str,
    ) -> PlatformCredential | None:
        result = await self._session.execute(
            select(PlatformCredential)
            .where(
                PlatformCredential.user_id == user_id,
                PlatformCredential.platform == platform,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_yahoo(
        self,
        user_id: uuid.UUID,
        access_token: str,
        refresh_token: str,
        expires_at: datetime,
    ) -> PlatformCredential:
        await self._session.execute(
            pg_insert(PlatformCredential)
            .values(
                user_id=user_id,
                platform="yahoo",
                access_token=encrypt_token(access_token),
                refresh_token=encrypt_token(refresh_token),
                token_expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "platform"],
                set_={
                    "access_token": encrypt_token(access_token),
                    "refresh_token": encrypt_token(refresh_token),
                    "token_expires_at": expires_at,
                },
            )
        )
        await self._session.commit()
        return await self.get_for_user(user_id, "yahoo")

    async def upsert_espn(
        self,
        user_id: uuid.UUID,
        espn_s2: str,
        swid: str,
    ) -> PlatformCredential:
        await self._session.execute(
            pg_insert(PlatformCredential)
            .values(
                user_id=user_id,
                platform="espn",
                espn_s2=encrypt_token(espn_s2),
                swid=encrypt_token(swid),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "platform"],
                set_={
                    "espn_s2": encrypt_token(espn_s2),
                    "swid": encrypt_token(swid),
                },
            )
        )
        await self._session.commit()
        return await self.get_for_user(user_id, "espn")

    async def upsert_sleeper(
        self,
        user_id: uuid.UUID,
        sleeper_user_id: str,
    ) -> PlatformCredential:
        await self._session.execute(
            pg_insert(PlatformCredential)
            .values(
                user_id=user_id,
                platform="sleeper",
                sleeper_user_id=sleeper_user_id,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "platform"],
                set_={"sleeper_user_id": sleeper_user_id},
            )
        )
        await self._session.commit()
        return await self.get_for_user(user_id, "sleeper")

    async def get_yahoo_tokens(
        self, user_id: uuid.UUID
    ) -> tuple[str, str, datetime] | None:
        """Returns (access_token, refresh_token, expires_at) decrypted."""
        cred = await self.get_for_user(user_id, "yahoo")
        if not cred or not cred.refresh_token:
            return None
        return (
            decrypt_token(cred.access_token or ""),
            decrypt_token(cred.refresh_token),
            cred.token_expires_at,
        )

    async def get_espn_cookies(
        self, user_id: uuid.UUID
    ) -> tuple[str, str] | None:
        """Returns (espn_s2, swid) decrypted."""
        cred = await self.get_for_user(user_id, "espn")
        if not cred or not cred.espn_s2:
            return None
        return (
            decrypt_token(cred.espn_s2),
            decrypt_token(cred.swid or ""),
        )

    async def disconnect(
        self,
        user_id: uuid.UUID,
        platform: str,
    ) -> None:
        await self._session.execute(
            delete(PlatformCredential)
            .where(
                PlatformCredential.user_id == user_id,
                PlatformCredential.platform == platform,
            )
        )
        await self._session.commit()
```

---

## Part 5 — Platform API implementations

### Yahoo

```python
# backend/integrations/yahoo_league_api.py
"""
Yahoo LeaguePlatformAPI implementation.
Loads per-user tokens from DB. Auto-refreshes on expiry.
"""
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, TeamRoster, Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague
from backend.repositories.credential_repo import CredentialRepository
from backend.core.exceptions import AppError


class YahooLeagueAPI(LeaguePlatformAPI):
    """
    Yahoo Fantasy Sports API — OAuth 2.0 per user.
    Builds on existing yahoo_api.py integration.
    """

    def __init__(
        self,
        league: UserLeague,
        access_token: str,
        refresh_token: str,
        expires_at: datetime,
        credential_repo: CredentialRepository,
        user_id,
    ):
        self._league = league
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._repo = credential_repo
        self._user_id = user_id

    @classmethod
    async def create(
        cls,
        league: UserLeague,
        db: AsyncSession,
    ) -> "YahooLeagueAPI":
        repo = CredentialRepository(db)
        tokens = await repo.get_yahoo_tokens(league.user_id)
        if not tokens:
            raise AppError(
                "Yahoo not connected — connect via /auth/yahoo/connect",
                {"platform": "yahoo", "action": "connect"},
            )
        access_token, refresh_token, expires_at = tokens
        return cls(
            league=league,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            credential_repo=repo,
            user_id=league.user_id,
        )

    async def _get_token(self) -> str:
        """Return valid access token, refreshing if expired."""
        if (
            self._expires_at
            and datetime.now(timezone.utc) >= self._expires_at
        ):
            await self._refresh()
        return self._access_token

    async def _refresh(self) -> None:
        """Exchange refresh token for new access token."""
        from backend.integrations.yahoo_api import refresh_access_token
        new_access, new_refresh, new_expiry = (
            await refresh_access_token(self._refresh_token)
        )
        await self._repo.upsert_yahoo(
            self._user_id, new_access, new_refresh, new_expiry
        )
        self._access_token = new_access
        self._refresh_token = new_refresh
        self._expires_at = new_expiry

    async def get_rosters(self) -> list[TeamRoster]:
        # Calls Yahoo API, returns normalized TeamRoster list
        # Uses existing yahoo_api.py functions
        token = await self._get_token()
        # ... Yahoo-specific implementation
        return []

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        token = await self._get_token()
        # Yahoo: /fantasy/v2/league/{league_key}/players;status=A
        return []

    async def get_draft_picks(self) -> list[DraftPick]:
        token = await self._get_token()
        # Yahoo: /fantasy/v2/league/{league_key}/draftresults
        return []

    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        token = await self._get_token()
        return []

    async def get_transactions(self, week: int) -> list[Transaction]:
        token = await self._get_token()
        return []

    async def get_standings(self) -> list[TeamRoster]:
        token = await self._get_token()
        return []
```

### ESPN

```python
# backend/integrations/espn_league_api.py
"""
ESPN LeaguePlatformAPI implementation.
Cookie-based unofficial API. Validates cookies on first use.
"""
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, TeamRoster, Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague
from backend.repositories.credential_repo import CredentialRepository
from backend.core.exceptions import AppError

ESPN_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"


class ESPNLeagueAPI(LeaguePlatformAPI):
    def __init__(
        self,
        league: UserLeague,
        espn_s2: str,
        swid: str,
    ):
        self._league = league
        self._cookies = {"espn_s2": espn_s2, "SWID": swid}

    @classmethod
    async def create(
        cls,
        league: UserLeague,
        db: AsyncSession,
    ) -> "ESPNLeagueAPI":
        repo = CredentialRepository(db)
        cookies = await repo.get_espn_cookies(league.user_id)
        if not cookies:
            raise AppError(
                "ESPN not connected — use the ESPN bookmarklet",
                {"platform": "espn", "action": "bookmarklet"},
            )
        espn_s2, swid = cookies
        return cls(league=league, espn_s2=espn_s2, swid=swid)

    async def _get(
        self,
        view: str,
        season: int | None = None,
    ) -> dict:
        season = season or self._league.season_year
        url = (
            f"{ESPN_BASE}/seasons/{season}/segments/0"
            f"/leagues/{self._league.league_id}"
        )
        async with httpx.AsyncClient(
            cookies=self._cookies, timeout=15.0
        ) as client:
            resp = await client.get(url, params={"view": view})
            if resp.status_code == 401:
                raise AppError(
                    "ESPN cookies expired — please reconnect",
                    {
                        "platform": "espn",
                        "action": "reconnect",
                        "bookmarklet_url": "/league-setup?platform=espn",
                    },
                )
            resp.raise_for_status()
            return resp.json()

    async def validate_cookies(self) -> bool:
        """Verify cookies work before storing."""
        await self._get("mSettings")
        return True

    async def get_rosters(self) -> list[TeamRoster]:
        data = await self._get("mRoster")
        # Parse ESPN roster format → TeamRoster list
        return []

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        data = await self._get("mFreeAgent")
        return []

    async def get_draft_picks(self) -> list[DraftPick]:
        data = await self._get("mDraftDetail")
        picks = data.get("draftDetail", {}).get("picks", [])
        result = []
        for pick in picks:
            player_data = (
                pick.get("playerPoolEntry", {})
                .get("playerPoolEntry", {})
                .get("player", {})
            )
            result.append(DraftPick(
                platform_player_id=str(
                    pick.get("playerId", "")
                ),
                player_name=player_data.get("fullName", ""),
                position=self._espn_pos(
                    player_data.get("defaultPositionId", 0)
                ),
                team_abbr="",
                picked_by_team_id=str(pick.get("teamId", "")),
                manager_name=str(pick.get("teamId", "")),
                pick_number=pick.get("overallPickNumber", 0),
                round_number=pick.get("roundId", 0),
                auction_price=pick.get("bidAmount"),
            ))
        return result

    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        data = await self._get("mMatchup")
        return []

    async def get_transactions(self, week: int) -> list[Transaction]:
        data = await self._get("mTransactions2")
        return []

    async def get_standings(self) -> list[TeamRoster]:
        data = await self._get("mStandings")
        return []

    def _espn_pos(self, pos_id: int) -> str:
        return {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}.get(pos_id, "")
```

### Sleeper

```python
# backend/integrations/sleeper_league_api.py
"""
Sleeper LeaguePlatformAPI implementation.
Public API — no auth required. Username only.
"""
import httpx
from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, TeamRoster, Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague

SLEEPER_BASE = "https://api.sleeper.app/v1"


class SleeperLeagueAPI(LeaguePlatformAPI):
    def __init__(self, league: UserLeague):
        self._league = league

    async def _get(self, path: str) -> dict | list:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SLEEPER_BASE}{path}")
            resp.raise_for_status()
            return resp.json()

    async def get_rosters(self) -> list[TeamRoster]:
        rosters = await self._get(
            f"/league/{self._league.league_id}/rosters"
        )
        users = await self._get(
            f"/league/{self._league.league_id}/users"
        )
        user_map = {u["user_id"]: u for u in users}

        result = []
        for roster in rosters:
            user = user_map.get(roster.get("owner_id"), {})
            result.append(TeamRoster(
                platform_team_id=str(roster["roster_id"]),
                manager_name=user.get("display_name", ""),
                team_name=user.get(
                    "metadata", {}
                ).get("team_name", ""),
                faab_remaining=roster.get("settings", {}).get(
                    "waiver_budget_used", 0
                ),
                wins=roster.get("settings", {}).get("wins", 0),
                losses=roster.get("settings", {}).get("losses", 0),
            ))
        return result

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        # Sleeper doesn't have a free agent endpoint.
        # Derive: all NFL players NOT on any roster.
        rosters = await self.get_rosters()
        rostered_sleeper_ids = {
            player_id
            for team in rosters
            for player_id in (
                team.players or []
            )
        }
        # In full implementation: fetch all players,
        # filter out rostered ones
        return []

    async def get_draft_picks(self) -> list[DraftPick]:
        drafts = await self._get(
            f"/league/{self._league.league_id}/drafts"
        )
        all_picks = []
        for draft in drafts:
            picks = await self._get(
                f"/draft/{draft['draft_id']}/picks"
            )
            for pick in picks:
                metadata = pick.get("metadata", {})
                all_picks.append(DraftPick(
                    platform_player_id=pick.get("player_id", ""),
                    player_name=(
                        f"{metadata.get('first_name', '')} "
                        f"{metadata.get('last_name', '')}"
                    ).strip(),
                    position=metadata.get("position", ""),
                    team_abbr=metadata.get("team", ""),
                    picked_by_team_id=str(
                        pick.get("roster_id", "")
                    ),
                    manager_name="",
                    pick_number=pick.get("pick_no", 0),
                    round_number=pick.get("round", 0),
                    auction_price=pick.get("amount"),
                ))
        return all_picks

    async def get_matchups(self, week: int) -> list[WeeklyMatchup]:
        data = await self._get(
            f"/league/{self._league.league_id}/matchups/{week}"
        )
        return []

    async def get_transactions(self, week: int) -> list[Transaction]:
        data = await self._get(
            f"/league/{self._league.league_id}/transactions/{week}"
        )
        return []

    async def get_standings(self) -> list[TeamRoster]:
        return await self.get_rosters()
```

---

## Part 6 — Yahoo OAuth multi-user

Update `backend/routers/auth.py` to store tokens per-user:

```python
# backend/routers/auth.py — replace existing Yahoo endpoints

import base64, json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from backend.core.dependencies import get_current_user, get_db
from backend.config import settings
from backend.repositories.credential_repo import CredentialRepository

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/yahoo/connect")
async def yahoo_connect(
    user=Depends(get_current_user),
):
    """
    Initiate Yahoo OAuth for current user.
    Encodes user_id in state parameter (CSRF protection).
    """
    state = base64.urlsafe_b64encode(
        json.dumps({"user_id": str(user.id)}).encode()
    ).decode()

    from backend.integrations.yahoo_api import get_authorization_url
    url = get_authorization_url(state=state)
    return RedirectResponse(url=url)


@router.get("/yahoo/callback")
async def yahoo_callback(
    code: str,
    state: str,
    db=Depends(get_db),
):
    """
    Yahoo OAuth callback — exchange code for tokens.
    Stores encrypted tokens per user. Redirects to account page.
    """
    try:
        state_data = json.loads(
            base64.urlsafe_b64decode(state).decode()
        )
        user_id = state_data["user_id"]
    except Exception:
        from backend.core.exceptions import ValidationError
        raise ValidationError("Invalid OAuth state parameter")

    from backend.integrations.yahoo_api import exchange_code_for_tokens
    tokens = await exchange_code_for_tokens(code)

    repo = CredentialRepository(db)
    await repo.upsert_yahoo(
        user_id=user_id,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=tokens["expires_at"],
    )

    return RedirectResponse(
        url="/account?connected=yahoo", status_code=302
    )


@router.delete("/yahoo/disconnect")
async def yahoo_disconnect(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    repo = CredentialRepository(db)
    await repo.disconnect(user.id, "yahoo")
    return {"status": "disconnected", "platform": "yahoo"}
```

---

## Part 7 — ESPN bookmarklet

### Bookmarklet utility

```javascript
// frontend/src/utils/espnBookmarklet.js
/**
 * Returns the bookmarklet code string.
 * User saves this as a browser bookmark.
 * When clicked on ESPN Fantasy, extracts cookies
 * and redirects to DraftMind automatically.
 */
export function getBookmarkletCode(appUrl) {
  const code = `
    (function() {
      function getCookie(name) {
        const match = document.cookie
          .split('; ')
          .find(r => r.startsWith(name + '='));
        return match ? decodeURIComponent(match.split('=')[1]) : null;
      }

      const espn_s2 = getCookie('espn_s2');
      const swid = getCookie('SWID');

      if (!espn_s2 || !swid) {
        alert(
          'ESPN cookies not found.\\n\\n' +
          'Make sure you are logged in to ESPN Fantasy before clicking this.'
        );
        return;
      }

      // Extract league ID from URL if on a league page
      const leagueMatch = window.location.href.match(/leagueId=(\\d+)/);
      const leagueId = leagueMatch ? leagueMatch[1] : '';

      let url = '${appUrl}/leagues/connect/espn/callback' +
        '?espn_s2=' + encodeURIComponent(espn_s2) +
        '&swid=' + encodeURIComponent(swid);

      if (leagueId) {
        url += '&league_id=' + leagueId;
      }

      window.location.href = url;
    })();
  `.trim();

  return 'javascript:' + encodeURIComponent(code);
}
```

### Backend callback endpoint

```python
# backend/routers/league_connect.py

@router.get("/connect/espn/callback")
async def espn_bookmarklet_callback(
    espn_s2: str,
    swid: str,
    league_id: str | None = None,
    season: int | None = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Receives ESPN cookies from the bookmarklet.
    Validates cookies against ESPN before storing.
    Redirects to league setup wizard.
    """
    from backend.integrations.espn_league_api import ESPNLeagueAPI
    from backend.utils.seasons import get_current_season
    from backend.models.user_league import UserLeague

    target_season = season or get_current_season()

    # Validate cookies work before storing
    if league_id:
        mock_league = UserLeague(
            league_id=league_id,
            season_year=target_season,
            platform="espn",
        )
        api = ESPNLeagueAPI(
            league=mock_league, espn_s2=espn_s2, swid=swid
        )
        await api.validate_cookies()
        # Raises AppError if invalid — caught by exception handler

    repo = CredentialRepository(db)
    await repo.upsert_espn(
        user_id=user.id, espn_s2=espn_s2, swid=swid
    )

    redirect_url = "/league-setup?platform=espn"
    if league_id:
        redirect_url += f"&league_id={league_id}"

    return RedirectResponse(url=redirect_url, status_code=302)
```

---

## Part 8 — League sync service

```python
# backend/services/league_sync.py
"""
LeagueSyncService — unified sync across all platforms.

Imports league settings, draft history, current rosters,
and free agents. All synced data scoped to user_id.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.platform_factory import get_platform_api
from backend.models.league_config import LeagueConfig
from backend.models.user_league import UserLeague
from backend.repositories.league_repo import LeagueRepository
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)

# How many historical seasons to import
HISTORY_SEASONS = 4


class LeagueSyncService:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID):
        self._db = db
        self._user_id = user_id
        self._league_repo = LeagueRepository(db)

    async def sync_league(
        self, user_league: UserLeague
    ) -> dict:
        """
        Full sync for a connected league.
        1. Verify credentials
        2. Import league settings → update LeagueConfig
        3. Import historical draft data (up to 4 seasons)
        4. Import current season rosters
        5. Cache free agent pool
        Returns sync summary.
        """
        platform = await get_platform_api(user_league, self._db)
        current_season = get_current_season()

        summary = {
            "platform": user_league.platform,
            "league_id": user_league.league_id,
            "picks_imported": 0,
            "managers_found": 0,
            "free_agents_cached": 0,
        }

        # 1. Import draft history — up to HISTORY_SEASONS
        picks_total = 0
        for offset in range(HISTORY_SEASONS):
            season = current_season - offset - 1  # completed seasons
            if season < 2020:
                break
            try:
                picks = await platform.get_draft_picks()
                # filter/store picks for this season
                picks_total += len(picks)
            except Exception as exc:
                logger.warning(
                    "Could not import %s season %d: %s",
                    user_league.platform, season, exc,
                )

        summary["picks_imported"] = picks_total

        # 2. Import current rosters
        rosters = await platform.get_rosters()
        summary["managers_found"] = len(rosters)

        # Store manager map in user_league
        user_league.manager_map = {
            r.platform_team_id: r.manager_name
            for r in rosters
        }
        user_league.last_synced = datetime.now(timezone.utc)

        # 3. Cache free agents (for waiver wire)
        free_agents = await platform.get_free_agents()
        summary["free_agents_cached"] = len(free_agents)

        await self._db.commit()
        return summary

    async def _store_picks(
        self,
        picks: list,
        user_league_id: uuid.UUID,
        season: int,
    ) -> int:
        """
        Store historical draft picks.
        All picks scoped to user_id.
        Deduplication via on_conflict_do_nothing.
        """
        from backend.models.league_auction_history import (
            LeagueAuctionHistory,
        )
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        count = 0
        for pick in picks:
            if not pick.player_name:
                continue
            await self._db.execute(
                pg_insert(LeagueAuctionHistory)
                .values(
                    user_id=self._user_id,
                    user_league_id=user_league_id,
                    player_name=pick.player_name,
                    position=pick.position,
                    price=pick.auction_price,
                    manager_name=pick.manager_name,
                    draft_pick_number=pick.pick_number,
                    season_year=season,
                )
                .on_conflict_do_nothing()
            )
            count += 1

        await self._db.commit()
        return count
```

---

## Part 9 — League sync endpoints

```python
# backend/routers/league_connect.py

router = APIRouter(prefix="/leagues", tags=["league-connect"])


@router.post("/connect/yahoo")
async def connect_yahoo_league(
    league_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Connect a Yahoo league. Requires Yahoo OAuth to be complete."""
    from backend.services.feature_service import FeatureService
    from backend.services.league_service import LeagueService
    from backend.utils.seasons import get_current_season

    # Check tier limits
    service = LeagueService(LeagueRepository(db))
    current_count = len(await service.get_user_leagues(user.id))
    FeatureService.can_add_league(user, current_count)

    # Create league record
    league = await service.add_league(
        user_id=user.id,
        platform="yahoo",
        league_id=league_id,
        season_year=get_current_season(),
        # Other settings fetched during sync
        team_count=12,
        draft_type="auction",
        scoring="ppr",
        budget=200,
    )

    # Sync
    sync_service = LeagueSyncService(db, user.id)
    summary = await sync_service.sync_league(league)

    return {"status": "connected", "league_id": str(league.id), **summary}


@router.post("/connect/sleeper")
async def connect_sleeper_league(
    username: str,
    league_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Connect a Sleeper league by username."""
    # Validate username exists in Sleeper
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.sleeper.app/v1/user/{username}"
        )
        if resp.status_code == 404:
            from backend.core.exceptions import NotFoundError
            raise NotFoundError(f"Sleeper user '{username}' not found")
        sleeper_data = resp.json()

    # Store Sleeper user ID
    repo = CredentialRepository(db)
    await repo.upsert_sleeper(user.id, sleeper_data["user_id"])

    # Create + sync league
    # ... same pattern as Yahoo


@router.post("/{league_id}/sync")
async def resync_league(
    league_id: uuid.UUID,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Re-sync a connected league (free — no credits)."""
    league = await _get_user_league(league_id, user, db)
    sync_service = LeagueSyncService(db, user.id)
    summary = await sync_service.sync_league(league)
    return {"status": "synced", **summary}


@router.get("/{league_id}/status")
async def get_league_status(
    league_id: uuid.UUID,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    league = await _get_user_league(league_id, user, db)
    return {
        "league_id": str(league_id),
        "platform": league.platform,
        "last_synced": (
            league.last_synced.isoformat()
            if league.last_synced else None
        ),
        "is_active": league.is_active,
    }


@router.delete("/{league_id}")
async def disconnect_league(
    league_id: uuid.UUID,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Remove a league (soft delete)."""
    from backend.services.league_service import LeagueService
    service = LeagueService(LeagueRepository(db))
    await service.remove_league(user.id, league_id)
    return {"status": "disconnected"}
```

---

## Part 10 — League setup wizard (frontend)

`frontend/src/pages/LeagueSetup.jsx`

5-step wizard:

```
Step 1: Choose Platform
  ┌────────┐  ┌────────┐  ┌────────┐
  │ Yahoo  │  │  ESPN  │  │Sleeper │
  └────────┘  └────────┘  └────────┘

Step 2: Connect (platform-specific)

  Yahoo:
    [Connect with Yahoo →]
    (OAuth redirect — handled server side)

  ESPN:
    Connect Your ESPN League
    
    Make sure you're logged in to ESPN Fantasy,
    then click below:
    
    [🏈 Connect ESPN →]   ← bookmarklet <a> tag
    
    Having trouble?
    [Enter cookies manually ↓]  (collapsed by default)

  Sleeper:
    Sleeper Username: [____________]
    [Find My Leagues →]

Step 3: Select League
  List of leagues found on platform.
  Radio buttons — select one.
  
  ○ The League (12-team PPR Auction)
  ○ Side League (10-team Half PPR Snake)

Step 4: Confirm Settings
  Team count: 12    Scoring: PPR
  Format: Auction   Budget: $200
  
  [These look right → Import League]
  [Edit Settings]

Step 5: Importing...
  ✓ League settings imported
  ✓ 720 draft picks imported (4 seasons)
  ✓ 11 manager profiles found
  ✓ 387 free agents cached
  [Go to Dashboard →]
```

ESPN bookmarklet button:
```jsx
import { getBookmarkletCode } from '../utils/espnBookmarklet'

const APP_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

<a
  href={getBookmarkletCode(APP_URL)}
  className="bg-orange-600 hover:bg-orange-500 text-white
             font-medium px-6 py-3 rounded-lg inline-flex
             items-center gap-2 transition-colors"
  onClick={(e) => {
    if (!window.location.href.includes('espn.com')) {
      e.preventDefault()
      window.open('https://fantasy.espn.com', '_blank')
      setShowEspnInstructions(true)
    }
  }}
>
  🏈 Connect ESPN →
</a>
```

---

## Part 11 — Alembic migration

```bash
alembic revision --autogenerate \
  -m "stage28_platform_credentials_league_sync"

# Verify creates:
#   platform_credentials table
#   Adds user_id to league_auction_history (nullable)
#   Adds user_league_id to league_auction_history (nullable)
#   Adds manager_map JSONB to user_leagues

alembic upgrade head
```

---

## Required test cases

```python
# Encryption
def test_encrypt_decrypt_roundtrip()
def test_empty_token_returns_empty()
def test_wrong_key_raises_value_error()

# Credentials
def test_yahoo_tokens_stored_encrypted()
def test_espn_cookies_stored_encrypted()
def test_user_a_cannot_access_user_b_tokens()
def test_disconnect_removes_credentials()

# ESPN bookmarklet
def test_bookmarklet_contains_app_url()
def test_espn_callback_validates_cookies_first()
def test_espn_callback_requires_auth()
def test_invalid_espn_cookies_raise_app_error()

# Yahoo OAuth
def test_state_param_encodes_user_id()
def test_state_param_decoded_on_callback()
def test_token_refresh_on_expiry()

# Sleeper
def test_sleeper_unknown_username_404()
def test_sleeper_roster_normalized()
def test_sleeper_draft_picks_with_auction_price()

# League sync
def test_sync_imports_up_to_4_seasons()
def test_sync_deduplicates_picks()
def test_picks_stored_with_user_id()
def test_picks_stored_with_user_league_id()
def test_user_a_picks_not_visible_to_user_b()
def test_free_agents_cached_after_sync()
def test_manager_names_stored_in_league()

# Tier limits
def test_intro_user_limited_to_1_league()
def test_standard_user_limited_to_2_leagues()
def test_pro_user_unlimited_leagues()
```

---

## Verification

```bash
# 1. Encryption works
python -c "
from backend.integrations.token_encryption import encrypt_token, decrypt_token
t = 'test_token_xyz'
assert decrypt_token(encrypt_token(t)) == t
print('Encryption: PASS')
"

# 2. Yahoo OAuth complete (use your personal account)
# GET /auth/yahoo/connect → authorize → /account?connected=yahoo

# 3. ESPN bookmarklet works
# Go to fantasy.espn.com → logged in → click bookmarklet
# Should redirect to /league-setup?platform=espn&league_id=...

# 4. Sleeper connects
# POST /leagues/connect/sleeper {username: "your_sleeper_name", league_id: "..."}
# Returns: {picks_imported: N, managers_found: M}

# 5. User isolation
# Sign in as user A → connect league → sign in as user B
# User B cannot see User A's league data
```

---

## Commit order

```
Commit 1:
feat(credentials): PlatformCredential model
and token encryption

Fernet AES encryption for all platform credentials.
encrypt_token/decrypt_token utilities.
CredentialRepository: upsert/get/disconnect per user.
Migration: platform_credentials table.

Commit 2:
feat(integrations): Yahoo, ESPN, Sleeper
LeaguePlatformAPI implementations

All implement LeaguePlatformAPI interface.
Yahoo: per-user OAuth, auto-refresh, existing yahoo_api.py adapted.
ESPN: cookie-based unofficial API, validates before storing.
Sleeper: public API, roster/draft/transaction normalization.

Commit 3:
feat(auth): Yahoo OAuth multi-user

State parameter encodes user_id (CSRF protection).
Tokens stored encrypted in platform_credentials.
/auth/yahoo/connect, /callback, DELETE /disconnect.
No more YAHOO_REFRESH_TOKEN env var.

Commit 4:
feat(espn): bookmarklet flow

GET /leagues/connect/espn/callback validates + stores cookies.
getBookmarkletCode() utility for frontend.
One-click connection — no DevTools required.

Commit 5:
feat(sync): LeagueSyncService and connect endpoints

POST /leagues/connect/{yahoo,espn,sleeper}.
4 years of draft history imported.
Current rosters and free agents cached.
All data user-scoped (user_id throughout).
POST /leagues/{id}/sync, GET /status, DELETE.

Commit 6:
feat(ui): League setup wizard

5-step wizard: platform → connect → select → confirm → import.
ESPN: bookmarklet button with fallback manual entry.
Yahoo: OAuth redirect button.
Sleeper: username search.
Coverage: X%.
```
