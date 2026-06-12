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
  DB sync        — sync_yahoo_player_ids
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

# NFL game_key → season mapping (verified from Yahoo API)
YAHOO_NFL_GAME_KEYS: dict[int, str] = {
    2026: "470", 2025: "461", 2024: "449", 2023: "423",
    2022: "414", 2021: "406", 2020: "399", 2019: "390",
    2018: "380", 2017: "371", 2016: "359",
}


def yahoo_league_key(league_id: str, season: int) -> str:
    """Construct Yahoo league key from league_id and season year.

    Raises ValueError for seasons without a known game key — a silent
    wrong-key fallback produces confusing 401/403s from Yahoo.
    """
    game_key = YAHOO_NFL_GAME_KEYS.get(season)
    if game_key is None:
        raise ValueError(
            f"Unknown Yahoo game key for season {season}. "
            f"Update YAHOO_NFL_GAME_KEYS in yahoo_api.py"
        )
    return f"{game_key}.l.{league_id}"

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

def get_authorization_url(state: str | None = None) -> str:
    """
    Return Yahoo OAuth2 authorization URL.

    Redirect the user's browser here. After granting access, Yahoo sends
    them back to YAHOO_REDIRECT_URI with ?code=... appended.

    Optional state parameter for CSRF protection (encodes user_id).
    """
    params = {
        "client_id": settings.yahoo_client_id,
        "redirect_uri": settings.yahoo_redirect_uri,
        "response_type": "code",
        "language": "en-us",
    }
    if state:
        params["state"] = state
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


async def refresh_access_token_for_user(
    refresh_token: str,
) -> tuple[str, str, "datetime"]:
    """
    Refresh an access token for a specific user's stored refresh_token.
    Returns (new_access_token, new_refresh_token, expires_at).
    Does NOT modify module-level token cache.
    """
    from datetime import datetime, timedelta, timezone

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

    expires_in = int(data.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    return (
        data["access_token"],
        data.get("refresh_token", refresh_token),
        expires_at,
    )


async def _get_valid_token() -> str:
    """Return a valid access token, refreshing if expired or absent."""
    global _cached_token, _token_expires_at
    if _cached_token and time.time() < _token_expires_at:
        return _cached_token
    return await refresh_access_token()


# ---------------------------------------------------------------------------
# Core API helper
# ---------------------------------------------------------------------------

async def _api_get_with_token(
    path: str, access_token: str, **extra_params: str
) -> dict[str, Any]:
    """Authenticated GET using an explicit per-user access token."""
    url = f"{_YAHOO_API_BASE}/{path.lstrip('/')}"
    params: dict[str, str] = {"format": "json"}
    params.update(extra_params)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


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
# Historical league discovery + draft data (available year-round)
# ---------------------------------------------------------------------------

async def get_all_user_leagues() -> list[dict[str, Any]]:
    """
    Discover all seasons of a league by following Yahoo's renew/renewed chain.

    Yahoo assigns a different league_id each season but links them via
    ``renew`` (previous season) and ``renewed`` (next season) fields.
    We start from a known league key and walk both directions.

    Requires YAHOO_LEAGUE_ID to be set. Tries recent game keys (2020-2026)
    to find the starting league, then follows the chain in both directions.

    Returns list of dicts sorted by season ascending:
        {league_key, league_id, name, season, num_teams, draft_type, is_auction}
    """
    league_id = settings.yahoo_league_id
    if not league_id:
        return []

    # Step 1: Find a valid starting league key
    start_key: str | None = None
    for year in sorted(YAHOO_NFL_GAME_KEYS.keys(), reverse=True):
        gk = YAHOO_NFL_GAME_KEYS[year]
        candidate = f"{gk}.l.{league_id}"
        try:
            data = await _api_get(f"league/{candidate}")
            league_info = data.get("fantasy_content", {}).get("league", [{}])[0]
            if league_info.get("league_key"):
                start_key = candidate
                logger.info("Found starting league: %s (season %s)", candidate, league_info.get("season"))
                break
        except Exception:
            continue  # 403/404 — this game_key doesn't have our league

    if not start_key:
        logger.warning("Could not find league_id=%s in any recent season", league_id)
        return []

    # Step 2: Walk the chain in both directions
    leagues: list[dict[str, Any]] = []
    visited: set[str] = set()

    async def _fetch_league(key: str) -> dict[str, Any] | None:
        if key in visited:
            return None
        visited.add(key)
        try:
            data = await _api_get(f"league/{key}")
            info = data.get("fantasy_content", {}).get("league", [{}])[0]
            if not info.get("league_key"):
                return None
            return info
        except Exception as e:
            logger.debug("Could not fetch league %s: %s", key, e)
            return None

    def _parse_link(link: str) -> str | None:
        """Parse 'game_key_league_id' format like '414_74752' → '414.l.74752'."""
        if not link:
            return None
        parts = link.split("_")
        if len(parts) == 2:
            return f"{parts[0]}.l.{parts[1]}"
        return None

    def _to_league_dict(info: dict) -> dict[str, Any]:
        return {
            "league_key": info.get("league_key"),
            "league_id": info.get("league_id"),
            "name": info.get("name"),
            "season": info.get("season"),
            "num_teams": info.get("num_teams"),
            "draft_type": info.get("draft_type"),
            "is_auction": str(info.get("draft_type", "")).lower() == "auction",
        }

    # Walk backwards via "renew"
    current = start_key
    while current:
        info = await _fetch_league(current)
        if not info:
            break
        leagues.append(_to_league_dict(info))
        current = _parse_link(info.get("renew", ""))

    # Walk forwards via "renewed" (starting from start_key's renewed)
    info = await _fetch_league(start_key)  # already visited, returns None
    # Re-fetch to get renewed link (we already have it in leagues)
    start_info = leagues[0] if leagues else None
    # Get renewed from the original start_key fetch
    try:
        data = await _api_get(f"league/{start_key}")
        first_info = data.get("fantasy_content", {}).get("league", [{}])[0]
        current = _parse_link(first_info.get("renewed", ""))
    except Exception:
        current = None

    while current:
        info = await _fetch_league(current)
        if not info:
            break
        leagues.append(_to_league_dict(info))
        current = _parse_link(info.get("renewed", ""))

    logger.info("Discovered %d seasons via league chain", len(leagues))
    return sorted(leagues, key=lambda x: int(x.get("season") or 0))


async def get_draft_results_for_league(league_key: str) -> list[dict[str, Any]]:
    """
    Pull complete draft results for a specific league (any season).

    Historical draft data is available year-round — no active league required.
    Returns list of: {pick, round, team_key, player_key, cost}
    """
    data = await _api_get(f"league/{league_key}/draftresults")
    content = data.get("fantasy_content", {}).get("league", [{}, {}])
    results_raw = content[1].get("draft_results", {}) if len(content) > 1 else {}

    picks: list[dict[str, Any]] = []
    for key, val in results_raw.items():
        if key == "count":
            continue
        pick = val.get("draft_result", {})
        picks.append({
            "pick": pick.get("pick"),
            "round": pick.get("round"),
            "team_key": pick.get("team_key"),
            "player_key": pick.get("player_key"),
            "cost": pick.get("cost"),
        })
    return picks


async def get_player_details_batch(player_keys: list[str]) -> list[dict[str, Any]]:
    """
    Resolve player keys to names, positions, and NFL teams.

    Yahoo allows up to 25 players per request.
    Returns list of: {player_key, name, position, nfl_team}
    """
    if not player_keys:
        return []

    keys_str = ",".join(player_keys[:25])
    data = await _api_get(f"players;player_keys={keys_str}")
    content = data.get("fantasy_content", {}).get("players", {})

    players: list[dict[str, Any]] = []
    for key, val in content.items():
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
        else:
            info = first

        name_data = info.get("name", {})
        full_name = name_data.get("full", "") if isinstance(name_data, dict) else ""
        position = ""
        display_position = info.get("display_position", "")
        if display_position:
            position = display_position.split(",")[0]  # Take primary position

        players.append({
            "player_key": info.get("player_key"),
            "name": full_name,
            "position": position,
            "nfl_team": info.get("editorial_team_abbr", ""),
        })

    return players


async def get_teams_in_league(league_key: str) -> list[dict[str, Any]]:
    """
    Get all teams with manager names for a specific league (any season).

    Returns list of: {team_key, team_name, manager_name}
    """
    data = await _api_get(f"league/{league_key}/teams")
    content = data.get("fantasy_content", {}).get("league", [{}, {}])
    teams_raw = content[1].get("teams", {}) if len(content) > 1 else {}

    teams: list[dict[str, Any]] = []
    for key, val in teams_raw.items():
        if key == "count":
            continue
        team_data = val.get("team", [{}])
        team_entry = team_data[0] if team_data else {}
        # Yahoo sometimes returns nested lists of field dicts — flatten
        if isinstance(team_entry, list):
            merged: dict[str, Any] = {}
            for item in team_entry:
                if isinstance(item, dict):
                    merged.update(item)
            team_entry = merged

        # Manager info can be nested in a managers dict
        manager_name = ""
        managers = team_entry.get("managers", {})
        if isinstance(managers, dict):
            for mkey, mval in managers.items():
                if mkey == "count":
                    continue
                mgr = mval.get("manager", {}) if isinstance(mval, dict) else {}
                manager_name = mgr.get("nickname", mgr.get("guid", ""))
                break  # Take first manager
        elif isinstance(managers, list) and managers:
            mgr = managers[0].get("manager", {}) if isinstance(managers[0], dict) else {}
            manager_name = mgr.get("nickname", mgr.get("guid", ""))

        teams.append({
            "team_key": team_entry.get("team_key"),
            "team_name": team_entry.get("name", ""),
            "manager_name": manager_name,
        })

    return teams


async def get_user_leagues(access_token: str) -> list[dict[str, Any]]:
    """
    Fetch all Fantasy Football leagues for the authenticated user.

    Uses a per-user access token (from platform_credentials).
    Yahoo API: GET /users;use_login=1/games;game_codes=nfl/leagues

    Returns list of dicts:
        league_key, league_id, name, season, num_teams,
        draft_type, scoring_type, is_finished, logo_url
    """
    from backend.utils.seasons import get_current_season

    data = await _api_get_with_token(
        "users;use_login=1/games;game_codes=nfl/leagues",
        access_token,
    )

    current = get_current_season()
    min_season = current - 1
    leagues: list[dict[str, Any]] = []

    # Navigate Yahoo's nested structure:
    #   fantasy_content.users.N.user = [user_info, {games: ...}]
    #   games.N.game = [game_info, {leagues: ...}]
    #   leagues.N.league = [league_info, ...]
    users = data.get("fantasy_content", {}).get("users", {})
    for ukey, uval in users.items():
        if ukey == "count":
            continue
        user_data = uval.get("user", [])
        if len(user_data) < 2 or not isinstance(user_data[1], dict):
            continue

        games = user_data[1].get("games", {})
        for gkey, gval in games.items():
            if gkey == "count":
                continue
            game_data = gval.get("game", [])
            if len(game_data) < 2:
                continue

            # game_data[1] has the leagues sub-resource
            league_container = game_data[1] if isinstance(game_data[1], dict) else {}
            league_dict = league_container.get("leagues", {})

            for lkey, lval in league_dict.items():
                if lkey == "count":
                    continue
                league_arr = lval.get("league", [])
                if not league_arr:
                    continue

                info = league_arr[0]
                # Yahoo sometimes nests fields as list of single-key dicts
                if isinstance(info, list):
                    merged: dict[str, Any] = {}
                    for item in info:
                        if isinstance(item, dict):
                            merged.update(item)
                    info = merged

                season = int(info.get("season", 0))
                if season < min_season:
                    continue

                leagues.append({
                    "league_key": info.get("league_key", ""),
                    "league_id": info.get("league_id", ""),
                    "name": info.get("name", ""),
                    "season": str(season),
                    "num_teams": int(info.get("num_teams", 0)),
                    "draft_type": info.get("draft_type", ""),
                    "scoring_type": info.get("scoring_type", ""),
                    "is_finished": bool(int(info.get("is_finished", 0) or 0)),
                    "logo_url": info.get("logo_url", ""),
                })

    # Unfinished current-season leagues first
    leagues.sort(key=lambda x: (-int(x["season"]), x["is_finished"]))
    logger.info("Found %d Yahoo leagues for user", len(leagues))
    return leagues


async def _detect_draft_type(
    access_token: str,
    league_key: str,
) -> tuple[str, int | None]:
    """
    Detect auction vs snake by checking draft results for cost data.

    Yahoo's draft_type field returns "live" for both auction and snake.
    The only reliable signal is whether picks have cost > 0.

    Returns:
        ("auction", budget) if any pick has cost > 0
        ("snake", None) if no costs found or detection fails
    """
    try:
        data = await _api_get_with_token(
            f"league/{league_key}/draftresults", access_token
        )
        content = data.get("fantasy_content", {}).get("league", [{}, {}])
        results_raw = (
            content[1].get("draft_results", {})
            if len(content) > 1
            else {}
        )

        has_costs = False
        for key, val in results_raw.items():
            if key == "count":
                continue
            pick = val.get("draft_result", {})
            cost = pick.get("cost")
            if cost is not None and int(cost) > 0:
                has_costs = True
                break

        if has_costs:
            logger.info(
                "Auction detected for %s (picks have cost data)",
                league_key,
            )
            return "auction", 200  # Yahoo default budget
        logger.info(
            "Snake detected for %s (no cost data in picks)",
            league_key,
        )
        return "snake", None
    except Exception as exc:
        logger.warning(
            "Draft type detection failed for %s: %s — defaulting to snake",
            league_key, exc,
        )
        return "snake", None


async def get_league_settings(
    access_token: str,
    league_key: str,
) -> dict[str, Any]:
    """
    Fetch full league settings from Yahoo Fantasy API.

    Endpoint: GET /league/{league_key}/settings?format=json

    Determines scoring format from stat modifiers:
      stat_id "11" = receptions
      weight 1.0 → ppr, 0.5 → half_ppr, 0.0 → standard

    Returns dict with: name, num_teams, draft_type, scoring_type,
    auction_budget, trade_deadline, waiver_type, playoff_start_week, uses_faab
    """
    data = await _api_get_with_token(
        f"league/{league_key}/settings", access_token
    )

    # Navigate: fantasy_content.league = [league_info, {settings: ...}]
    league_arr = data.get("fantasy_content", {}).get("league", [])

    # League metadata is in element 0
    league_meta: dict[str, Any] = {}
    if league_arr:
        first = league_arr[0]
        if isinstance(first, list):
            for item in first:
                if isinstance(item, dict):
                    league_meta.update(item)
        elif isinstance(first, dict):
            league_meta = first

    # Settings sub-resource is in element 1
    settings_data: dict[str, Any] = {}
    if len(league_arr) > 1 and isinstance(league_arr[1], dict):
        settings_data = league_arr[1].get("settings", [{}])
        if isinstance(settings_data, list) and settings_data:
            merged: dict[str, Any] = {}
            for item in settings_data:
                if isinstance(item, dict):
                    merged.update(item)
            settings_data = merged

    logger.info(
        "Yahoo league settings raw: meta=%s settings_keys=%s",
        {k: league_meta.get(k) for k in ("name", "num_teams", "draft_type",
         "scoring_type", "is_finished", "season")},
        list(settings_data.keys()) if isinstance(settings_data, dict) else "N/A",
    )

    # Determine scoring from stat modifiers
    stat_mods_raw = settings_data.get("stat_modifiers", {})
    stat_list = stat_mods_raw.get("stats", []) if isinstance(stat_mods_raw, dict) else []
    # Yahoo nests as: stat_modifiers.stats = [{stat: {stat_id, value}}, ...]
    stat_mods: list[dict[str, Any]] = []
    for entry in stat_list:
        if isinstance(entry, dict):
            stat = entry.get("stat", entry)
            stat_mods.append(stat)

    reception_mod = next(
        (
            float(m.get("value", 0))
            for m in stat_mods
            if str(m.get("stat_id")) == "11"
        ),
        0.0,
    )
    if reception_mod >= 1.0:
        scoring_type = "ppr"
    elif reception_mod >= 0.5:
        scoring_type = "half_ppr"
    else:
        scoring_type = "standard"

    # Draft type — detect from actual draft pick costs, not raw metadata
    # Yahoo returns "live" for both auction and snake drafts
    draft_type, auction_budget = await _detect_draft_type(
        access_token, league_key
    )

    # Waiver type
    waiver_rule = str(settings_data.get("waiver_type", "")).lower()
    uses_faab = "faab" in waiver_rule or waiver_rule == "2"

    return {
        "name": league_meta.get("name", ""),
        "num_teams": int(league_meta.get("num_teams", 12)),
        "draft_type": draft_type,
        "scoring_type": scoring_type,
        "auction_budget": auction_budget,
        "trade_deadline": settings_data.get("trade_end_date", ""),
        "waiver_type": "faab" if uses_faab else waiver_rule,
        "playoff_start_week": int(
            settings_data.get("playoff_start_week", 15)
        ),
        "uses_faab": uses_faab,
    }


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


