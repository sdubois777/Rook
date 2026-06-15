"""Guard tests for the browser extension's static configuration.

The extension is loaded unpacked from extension/dist/, talks only to
the production backend, and must inject yahoo_auth on both Yahoo
Fantasy hosts. These tests pin those facts so a future edit cannot
silently regress them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

EXTENSION_DIR = Path(__file__).resolve().parents[2] / "extension"

PRODUCTION_API_BASE = "https://fantasymanager-production.up.railway.app"


def _manifest() -> dict:
    return json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))


def test_get_api_base_returns_production_url():
    """getApiBase() must return the Railway URL — no localhost, no dev split."""
    source = (EXTENSION_DIR / "src" / "utils" / "api.js").read_text(encoding="utf-8")

    match = re.search(r"export function getApiBase\(\)[^}]+\}", source)
    assert match, "getApiBase() not found in extension/src/utils/api.js"
    body = match.group(0)

    assert PRODUCTION_API_BASE in body
    assert "localhost" not in body


def test_manifest_background_path_has_no_dist_prefix():
    """Background is a dist-relative MV3 service worker.

    Chrome MV3 rejects "scripts" and "type" under background, and webpack
    bundles to a single file, so only "service_worker" is present (Firefox
    support via "scripts" is deferred).
    """
    background = _manifest()["background"]

    assert background["service_worker"] == "background.js"
    assert "scripts" not in background
    assert "type" not in background


def test_yahoo_auth_matches_sports_yahoo():
    """yahoo_auth injects on both the classic and modern Yahoo Fantasy hosts."""
    yahoo_auth_matches = [
        pattern
        for cs in _manifest()["content_scripts"]
        if "yahoo_auth.js" in cs["js"]
        for pattern in cs["matches"]
    ]

    assert "https://sports.yahoo.com/fantasy/*" in yahoo_auth_matches
    assert "https://football.fantasysports.yahoo.com/*" in yahoo_auth_matches
