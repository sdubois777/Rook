"""
League connect router — connect and sync leagues from Yahoo, ESPN, Sleeper.

POST   /leagues/connect/yahoo           — connect Yahoo league
POST   /leagues/connect/espn            — connect ESPN league (manual cookies)
POST   /leagues/connect/sleeper         — connect Sleeper league
GET    /leagues/connect/espn/callback   — ESPN bookmarklet callback
POST   /leagues/{id}/sync              — re-sync a connected league
GET    /leagues/{id}/status            — sync status
DELETE /leagues/{id}                   — soft delete league
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from backend.core.dependencies import get_current_user, get_db
from backend.core.exceptions import NotFoundError, ValidationError
from backend.repositories.credential_repo import CredentialRepository
from backend.repositories.league_repo import LeagueRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/leagues", tags=["league-connect"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ConnectYahooRequest(BaseModel):
    league_id: str


class ConnectSleeperRequest(BaseModel):
    username: str
    league_id: str


class ConnectEspnRequest(BaseModel):
    league_id: str
    espn_s2: str
    swid: str
    season: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_user_league(league_id: uuid.UUID, user, db):
    """Get league verifying ownership."""
    repo = LeagueRepository(db)
    league = await repo.get_user_league(user.id, league_id)
    if not league:
        raise NotFoundError(f"League {league_id} not found")
    return league


# ---------------------------------------------------------------------------
# ESPN bookmarklet callback
# ---------------------------------------------------------------------------

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
            user_id=user.id,
            team_count=12,
            draft_type="auction",
            scoring="ppr",
        )
        api = ESPNLeagueAPI(
            league=mock_league, espn_s2=espn_s2, swid=swid
        )
        await api.validate_cookies()

    repo = CredentialRepository(db)
    await repo.upsert_espn(
        user_id=user.id, espn_s2=espn_s2, swid=swid
    )

    redirect_url = "/league-setup?platform=espn"
    if league_id:
        redirect_url += f"&league_id={league_id}"

    return RedirectResponse(url=redirect_url, status_code=302)


# ---------------------------------------------------------------------------
# Connect endpoints
# ---------------------------------------------------------------------------

@router.post("/connect/yahoo")
async def connect_yahoo_league(
    body: ConnectYahooRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Connect a Yahoo league. Requires Yahoo OAuth to be complete."""
    from backend.services.feature_service import FeatureService
    from backend.services.league_service import LeagueService
    from backend.services.league_sync import LeagueSyncService
    from backend.utils.seasons import get_current_season

    # Check tier limits
    league_repo = LeagueRepository(db)
    current_count = await league_repo.count_active(user.id)
    FeatureService.can_add_league(user, current_count)

    # Create league record
    service = LeagueService(league_repo)
    league = await service.add_league(
        user_id=user.id,
        platform="yahoo",
        league_id=body.league_id,
        season_year=get_current_season(),
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
    body: ConnectSleeperRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Connect a Sleeper league by username."""
    import httpx
    from backend.services.feature_service import FeatureService
    from backend.services.league_service import LeagueService
    from backend.services.league_sync import LeagueSyncService
    from backend.utils.seasons import get_current_season

    # Validate username exists
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://api.sleeper.app/v1/user/{body.username}"
        )
        if resp.status_code == 404 or resp.json() is None:
            raise NotFoundError(f"Sleeper user '{body.username}' not found")
        sleeper_data = resp.json()

    # Store Sleeper user ID
    repo = CredentialRepository(db)
    await repo.upsert_sleeper(user.id, sleeper_data["user_id"])

    # Check tier limits
    league_repo = LeagueRepository(db)
    current_count = await league_repo.count_active(user.id)
    FeatureService.can_add_league(user, current_count)

    # Create league record
    service = LeagueService(league_repo)
    league = await service.add_league(
        user_id=user.id,
        platform="sleeper",
        league_id=body.league_id,
        season_year=get_current_season(),
        team_count=12,
        draft_type="auction",
        scoring="ppr",
        budget=200,
    )

    # Sync
    sync_service = LeagueSyncService(db, user.id)
    summary = await sync_service.sync_league(league)

    return {"status": "connected", "league_id": str(league.id), **summary}


@router.post("/connect/espn")
async def connect_espn_league(
    body: ConnectEspnRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Connect ESPN league with manual cookie entry."""
    from backend.integrations.espn_league_api import ESPNLeagueAPI
    from backend.services.feature_service import FeatureService
    from backend.services.league_service import LeagueService
    from backend.services.league_sync import LeagueSyncService
    from backend.utils.seasons import get_current_season
    from backend.models.user_league import UserLeague

    target_season = body.season or get_current_season()

    # Validate cookies first
    mock_league = UserLeague(
        league_id=body.league_id,
        season_year=target_season,
        platform="espn",
        user_id=user.id,
        team_count=12,
        draft_type="auction",
        scoring="ppr",
    )
    api = ESPNLeagueAPI(
        league=mock_league, espn_s2=body.espn_s2, swid=body.swid
    )
    await api.validate_cookies()

    # Store cookies
    repo = CredentialRepository(db)
    await repo.upsert_espn(
        user_id=user.id, espn_s2=body.espn_s2, swid=body.swid
    )

    # Check tier limits
    league_repo = LeagueRepository(db)
    current_count = await league_repo.count_active(user.id)
    FeatureService.can_add_league(user, current_count)

    # Create league record
    service = LeagueService(league_repo)
    league = await service.add_league(
        user_id=user.id,
        platform="espn",
        league_id=body.league_id,
        season_year=target_season,
        team_count=12,
        draft_type="auction",
        scoring="ppr",
        budget=200,
    )

    # Sync
    sync_service = LeagueSyncService(db, user.id)
    summary = await sync_service.sync_league(league)

    return {"status": "connected", "league_id": str(league.id), **summary}


# ---------------------------------------------------------------------------
# Sync / status / delete
# ---------------------------------------------------------------------------

@router.post("/{league_id}/sync")
async def resync_league(
    league_id: uuid.UUID,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Re-sync a connected league (free — no credits)."""
    from backend.services.league_sync import LeagueSyncService

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
    """Get sync status for a league."""
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
