"""
scripts/test_yahoo_live.py

Live integration test for Yahoo Fantasy API.
Tests token refresh and get_players() against the real Yahoo API.
Run with: uv run python scripts/test_yahoo_live.py

Requirements:
  - YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REFRESH_TOKEN in .env
  - Internet connection

Does NOT test league-specific endpoints (get_league, get_teams, get_rosters,
get_draft_results) — those require an active league (~August).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.integrations.yahoo_api import (
    get_authorization_url,
    get_players,
    refresh_access_token,
)


async def main() -> None:
    print("=" * 60)
    print("Yahoo Fantasy API — Live Integration Test")
    print("=" * 60)

    # 1. OAuth URL generation (no network, always safe)
    print("\n[1] OAuth authorization URL")
    url = get_authorization_url()
    assert "api.login.yahoo.com" in url, "URL missing Yahoo domain"
    assert "response_type=code" in url, "URL missing response_type"
    print(f"    ✓  {url[:80]}...")

    # 2. Token refresh (real network call to Yahoo token endpoint)
    print("\n[2] Token refresh (YAHOO_REFRESH_TOKEN → access token)")
    try:
        token = await refresh_access_token()
        assert token and len(token) > 20, "Token too short — likely an error response"
        print(f"    ✓  Access token received ({len(token)} chars)")
    except Exception as exc:
        print(f"    ✗  FAILED: {exc}")
        print("       Check YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REFRESH_TOKEN in .env")
        sys.exit(1)

    # 3. get_players() — always available regardless of league status
    print("\n[3] get_players(count=25) — Yahoo player universe")
    try:
        players = await get_players(count=25)
        assert isinstance(players, list), "Expected a list"
        assert len(players) > 0, "Got empty player list"
        first = players[0]
        name = first.get("name", {}).get("full") or first.get("name", "?")
        pid = first.get("player_id", "?")
        print(f"    ✓  {len(players)} players returned")
        print(f"       First player: {name} (id={pid})")
        # Spot-check a few
        player_ids = [p.get("player_id") for p in players if p.get("player_id")]
        assert len(player_ids) >= 10, f"Too few player IDs: {len(player_ids)}"
        print(f"       Player IDs present: {len(player_ids)}/{len(players)}")
    except Exception as exc:
        print(f"    ✗  FAILED: {exc}")
        sys.exit(1)

    # 4. Verify name normalization round-trip (purely local)
    print("\n[4] Name normalization — ensure players can be matched to DB")
    from backend.integrations.nfl_data import normalize_player_name
    test_cases = [
        ("Patrick Mahomes", "patrick mahomes"),
        ("D.K. Metcalf", "dk metcalf"),
        ("Ja'Marr Chase", "jamarr chase"),
        ("Travis Kelce Jr.", "travis kelce"),
    ]
    for raw, expected in test_cases:
        result = normalize_player_name(raw)
        assert result == expected, f"normalize({raw!r}) = {result!r}, want {expected!r}"
    print(f"    ✓  {len(test_cases)} normalization cases pass")

    print("\n" + "=" * 60)
    print("All live tests PASSED")
    print("\nNOTE: League endpoints (get_league, get_teams, get_rosters,")
    print("      get_draft_results) are UNTESTABLE until league is active (~August).")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
