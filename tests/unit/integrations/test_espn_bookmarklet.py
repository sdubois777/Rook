"""Tests for ESPN bookmarklet utility."""


def test_bookmarklet_file_exists():
    """Verify the bookmarklet utility file was created."""
    from pathlib import Path
    bookmarklet_path = Path(__file__).resolve().parents[3] / (
        "frontend/src/utils/espnBookmarklet.js"
    )
    assert bookmarklet_path.exists()
    content = bookmarklet_path.read_text()
    assert "getBookmarkletCode" in content
    assert "espn_s2" in content
    assert "SWID" in content
    assert "connect/espn/callback" in content


def test_bookmarklet_uses_app_url_template():
    """Verify the bookmarklet injects the app URL."""
    from pathlib import Path
    bookmarklet_path = Path(__file__).resolve().parents[3] / (
        "frontend/src/utils/espnBookmarklet.js"
    )
    content = bookmarklet_path.read_text()
    # Template string should reference appUrl parameter
    assert "${appUrl}" in content


def test_bookmarklet_checks_for_missing_cookies():
    """Bookmarklet should alert if cookies not found."""
    from pathlib import Path
    bookmarklet_path = Path(__file__).resolve().parents[3] / (
        "frontend/src/utils/espnBookmarklet.js"
    )
    content = bookmarklet_path.read_text()
    assert "ESPN cookies not found" in content


def test_bookmarklet_extracts_league_id():
    """Bookmarklet should extract leagueId from ESPN URL."""
    from pathlib import Path
    bookmarklet_path = Path(__file__).resolve().parents[3] / (
        "frontend/src/utils/espnBookmarklet.js"
    )
    content = bookmarklet_path.read_text()
    assert "leagueId" in content
