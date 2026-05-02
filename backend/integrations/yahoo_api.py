"""
Yahoo Fantasy API integration — OAuth 2.0 + REST data pulls.

Auth flow (one-time setup):
  GET /auth/yahoo          → redirect user to Yahoo authorization page
  GET /auth/yahoo/callback → exchange code for tokens; log refresh token to .env

All subsequent calls auto-refresh the access token using YAHOO_REFRESH_TOKEN.
Access tokens are cached in memory (1-hour TTL minus 60-second buffer).

File layout:
  OAuth helpers  — get_authorization_url, exchange_code_for_tokens, refresh_access_token
  API calls      — get_league, get_teams, get_players, get_draft_results, get_rosters
  DB sync        — sync_yahoo_player_ids, sync_league_settings
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from backend.config import settings
from backend.integrations.nfl_data import normalize_player_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
_YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
_YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# ---------------------------------------------------------------------------
# In-memory token cache (refreshed automatically on expiry)
# ---------------------------------------------------------------------------

_cached_token: str | None = None
_token_expires_at: float = 0.0  # Unix epoch seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _league_key() -> str:
    """Return Yahoo league key derived from configured YAHOO_LEAGUE_ID."""
    return f"nfl.l.{settings.yahoo_league_id}"


def _basic_auth_header() -> str:
    """Base64-encoded Authorization header for OAuth token requests."""
    raw = f"{settings.yahoo_client_id}:{settings.yahoo_client_secret}"
    encoded = base64.b64encode(raw.encode()).decode()
    return f"Basic {encoded}"


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def get_authorization_url() -> str:
    """
    Return Yahoo OAuth2 authorization URL.

    Redirect the user's browser here. After granting access, Yahoo sends
    them back to YAHOO_REDIRECT_URI with ?code=... appended.
    """
    params = {
        "client_id": settings.yahoo_client_id,
        "redirect_uri": settings.yahoo_redirect_uri,
        "response_type": "code",
        "language": "en-us",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{_YAHOO_AUTH_URL}?{query}"


async def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    """
    Exchange OAuth authorization code for access + refresh tokens.

    Called once during initial setup via the /auth/yahoo/callback route.
    The returned refresh_token must be stored in .env as YAHOO_REFRESH_TOKEN.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _YAHOO_TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.yahoo_redirect_uri,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token() -> str:
    """
    Use YAHOO_REFRESH_TOKEN to obtain a new access token.
    Updates the module-level token cache.
    """
    global _cached_token, _token_expires_at

    refresh_token = settings.yahoo_refresh_token
    if not refresh_token:
        raise ValueError(
            "YAHOO_REFRESH_TOKEN not configured — complete OAuth flow at GET /auth/yahoo"
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _YAHOO_TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _cached_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _token_expires_at = time.time() + expires_in - 60  # 60s buffer
    logger.info("Yahoo access token refreshed (valid for %ds)", expires_in)
    return _cached_token


async def _get_valid_token() -> str:
    """Return a valid access token, refreshing if expired or absent."""
    global _cached_token, _token_expires_at
    if _cached_token and time.time() < _token_expires_at:
        return _cached_token
    return await refresh_access_token()


# ---------------------------------------------------------------------------
# Core API helper
# ---------------------------------------------------------------------------

async def _api_get(path: str, **extra_params: str) -> dict[str, Any]:
    """
    Authenticated GET against the Yahoo Fantasy API.
    Always requests JSON format; merges any extra query params.
    """
    token = await _get_valid_token()
    url = f"{_YAHOO_API_BASE}/{path.lstrip('/')}"
    params: dict[str, str] = {"format": "json"}
    params.update(extra_params)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Yahoo Fantasy API functions
# ---------------------------------------------------------------------------

# UNTESTABLE UNTIL LEAGUE ACTIVE (~August)
# League ID: stored in YAHOO_LEAGUE_ID env var
# These endpoints require an active Yahoo Fantasy league.
# Run POST /pipeline/sync-league-settings and POST /pipeline/sync-yahoo-players
# once the league is set up (typically late July / early August).

async def get_league() -> dict[str, Any]:
    """
    Return league metadata dict.
    Includes: name, scoring_type, num_teams, auction_draft, playoff_start_week, etc.

    UNTESTABLE UNTIL LEAGUE ACTIVE (~August) — requires YAHOO_LEAGUE_ID set in .env.
    """
    data = await _api_get(f"league/{_league_key()}")
    league_list = data.get("fantasy_content", {}).get("league", [])
    return league_list[0] if league_list else {}


async def get_teams() -> list[dict[str, Any]]:
    """
    Return list of team metadata dicts for all teams in the league.

    UNTESTABLE UNTIL LEAGUE ACTIVE (~August) — requires YAHOO_LEAGUE_ID set in .env.
    """
    data = await _api_get(f"league/{_league_key()}/teams")
    content = data.get("fantasy_content", {}).get("league", [{}, {}])
    teams_raw = content[1].get("teams", {}) if len(content) > 1 else {}

    teams: list[dict[str, Any]] = []
    for key, val in teams_raw.items():
        if key == "count":
            continue
        team_entry = val.get("team", [{}])[0]
        # Yahoo sometimes returns nested lists of field dicts — flatten them
        if isinstance(team_entry, list):
            merged: dict[str, Any] = {}
            for item in team_entry:
                if isinstance(item, dict):
                    merged.update(item)
            team_entry = merged
        teams.append(team_entry)

    return teams


async def get_players(count: int = 300) -> list[dict[str, Any]]:
    """
    Return up to `count` players from Yahoo's NFL player universe.

    Uses the game-level endpoint (/game/nfl/players) — available year-round,
    does NOT require an active league. Safe to call in the offseason.

    Paginates in batches of 25 (Yahoo's per-request maximum).
    """
    all_players: list[dict[str, Any]] = []
    page_size = 25
    start = 0

    while start < count:
        batch = min(page_size, count - start)
        data = await _api_get(f"game/nfl/players;start={start};count={batch}")
        content = data.get("fantasy_content", {}).get("game", [{}, {}])
        players_raw = content[1].get("players", {}) if len(content) > 1 else {}

        batch_players: list[dict[str, Any]] = []
        for key, val in players_raw.items():
            if key == "count":
                continue
            player_list = val.get("player", [{}])
            first = player_list[0] if player_list else {}
            # Yahoo nests player fields as a list of single-key dicts — flatten
            if isinstance(first, list):
                info: dict[str, Any] = {}
                for item in first:
                    if isinstance(item, dict):
                        info.update(item)
                batch_players.append(info)
            else:
                batch_players.append(first)

        if not batch_players:
            break
        all_players.extend(batch_players)
        start += len(batch_players)
        if len(batch_players) < batch:
            break  # Last page

    return all_players


async def get_draft_results() -> list[dict[str, Any]]:
    """
    Return list of draft pick dicts: {pick, round, team_key, player_key}.

    UNTESTABLE UNTIL LEAGUE ACTIVE (~August) — requires YAHOO_LEAGUE_ID set in .env
    and a completed draft.
    """
    data = await _api_get(f"league/{_league_key()}/draftresults")
    content = data.get("fantasy_content", {}).get("league", [{}, {}])
    results_raw = content[1].get("draft_results", {}) if len(content) > 1 else {}

    picks: list[dict[str, Any]] = []
    for key, val in results_raw.items():
        if key == "count":
            continue
        picks.append(val.get("draft_result", {}))
    return picks


async def get_rosters() -> dict[str, list[dict[str, Any]]]:
    """
    Return all team rosters keyed by team_key.
    {team_key: [player_dict, ...]}

    UNTESTABLE UNTIL LEAGUE ACTIVE (~August) — requires YAHOO_LEAGUE_ID set in .env
    and teams with active rosters.
    """
    data = await _api_get(f"league/{_league_key()}/teams//roster")
    content = data.get("fantasy_content", {}).get("league", [{}, {}])
    teams_raw = content[1].get("teams", {}) if len(content) > 1 else {}

    rosters: dict[str, list[dict[str, Any]]] = {}
    for key, val in teams_raw.items():
        if key == "count":
            continue
        team_data = val.get("team", [{}, {}])
        if len(team_data) < 2:
            continue
        team_info = team_data[0]
        team_key = (
            team_info.get("team_key", key)
            if isinstance(team_info, dict)
            else str(key)
        )
        roster_data = team_data[1].get("roster", {}).get("players", {})
        players: list[dict[str, Any]] = []
        for pkey, pval in roster_data.items():
            if pkey == "count":
                continue
            players.append(pval.get("player", [{}])[0])
        rosters[team_key] = players

    return rosters


# ---------------------------------------------------------------------------
# DB sync functions
# ---------------------------------------------------------------------------

async def sync_yahoo_player_ids(db_session) -> dict[str, int]:
    """
    Pull Yahoo player universe and match to DB player records by normalized name.
    Updates yahoo_player_id on matched rows with Yahoo's numeric player ID.
    Unmatched players are logged but do not raise exceptions.

    Returns: {"matched": N, "unmatched": M}
    """
    from sqlalchemy import select
    from backend.models.player import Player

    yahoo_players = await get_players(count=300)

    # Load all DB players
    result = await db_session.execute(select(Player))
    db_players: list[Player] = list(result.scalars().all())

    # Build normalized name → Player lookup
    name_to_player: dict[str, Player] = {}
    for p in db_players:
        if p.name:
            name_to_player[normalize_player_name(p.name)] = p

    matched = 0
    unmatched = 0

    for yp in yahoo_players:
        player_id = yp.get("player_id")
        name_data = yp.get("name", {})
        full_name = name_data.get("full", "") if isinstance(name_data, dict) else ""

        if not player_id or not full_name:
            continue

        norm = normalize_player_name(full_name)
        db_player = name_to_player.get(norm)

        if db_player:
            db_player.yahoo_player_id = str(player_id)
            matched += 1
        else:
            logger.info(
                "Yahoo player not matched in DB: %s (yahoo_id=%s)", full_name, player_id
            )
            unmatched += 1

    await db_session.commit()
    logger.info(
        "Yahoo player ID sync complete — matched=%d, unmatched=%d", matched, unmatched
    )
    return {"matched": matched, "unmatched": unmatched}


async def sync_league_settings(db_session) -> dict[str, Any]:
    """
    Pull league metadata from Yahoo and upsert into the league_settings table.
    Only updates fields that Yahoo provides — does not overwrite budget/valuation constants.

    Returns a summary dict of what was synced.
    """
    from sqlalchemy import select
    from backend.models.league_settings import LeagueSettings

    league = await get_league()

    scoring_type = str(league.get("scoring_type", "ppr")).lower()
    scoring_format = {
        "ppr": "PPR",
        "0.5ppr": "Half-PPR",
        "half": "Half-PPR",
        "standard": "Standard",
    }.get(scoring_type, "PPR")

    team_count = int(league.get("num_teams", 12))

    result = await db_session.execute(select(LeagueSettings).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        row = LeagueSettings(platform="Yahoo")
        db_session.add(row)

    row.scoring_format = scoring_format
    row.team_count = team_count
    # skill_starter_budget may be None on a brand-new row (column default only applies on INSERT)
    skill_budget = row.skill_starter_budget or 185
    row.league_skill_dollar_pool = int(skill_budget * team_count)

    await db_session.commit()
    logger.info(
        "League settings synced — name=%s, scoring=%s, teams=%d",
        league.get("name"),
        scoring_format,
        team_count,
    )

    return {
        "league_name": league.get("name"),
        "scoring_format": scoring_format,
        "team_count": team_count,
        "league_skill_dollar_pool": row.league_skill_dollar_pool,
    }
