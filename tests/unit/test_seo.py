"""SEO/AEO Tier 1 — structured data + crawler routes.

Guards the two things that must never silently drift:
  1. The static JSON-LD in frontend/index.html parses AND its SoftwareApplication
     offer prices equal backend/models/user.py (no second hardcoded price copy
     going stale).
  2. robots.txt / sitemap.xml / llms.txt are served as REAL files (not the SPA
     shell), AI crawlers are allowed, and llms.txt prices match user.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import TIER_LIMITS

_INDEX_HTML = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def _json_ld_blocks() -> list[dict]:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    raw = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    )
    return [json.loads(b) for b in raw]  # json.loads raises if any block is invalid


def _by_type(blocks, t):
    return next(b for b in blocks if b.get("@type") == t)


def test_index_has_three_valid_jsonld_blocks():
    blocks = _json_ld_blocks()  # parses → proves valid JSON
    types = {b.get("@type") for b in blocks}
    assert {"Organization", "SoftwareApplication", "FAQPage"} <= types
    for b in blocks:
        assert b.get("@context") == "https://schema.org"


def test_softwareapplication_offer_prices_match_user_py():
    """The schema prices are asserted equal to TIER_LIMITS — drift fails CI."""
    app_ld = _by_type(_json_ld_blocks(), "SoftwareApplication")
    prices = {o["name"]: o["price"] for o in app_ld["offers"]}
    assert prices["Free"] == str(TIER_LIMITS["free"]["price_monthly_usd"])
    assert prices["Standard — monthly"] == str(TIER_LIMITS["standard"]["price_monthly_usd"])
    assert prices["Standard — season"] == str(TIER_LIMITS["standard"]["price_season_usd"])
    assert prices["Pro — monthly"] == str(TIER_LIMITS["pro"]["price_monthly_usd"])
    assert prices["Pro — season"] == str(TIER_LIMITS["pro"]["price_season_usd"])
    for o in app_ld["offers"]:
        assert o["priceCurrency"] == "USD"


def test_faqpage_marks_up_every_landing_question():
    faq = _by_type(_json_ld_blocks(), "FAQPage")
    qs = faq["mainEntity"]
    assert len(qs) == 7
    for q in qs:
        assert q["@type"] == "Question"
        assert q["name"].strip()
        assert q["acceptedAnswer"]["@type"] == "Answer"
        assert q["acceptedAnswer"]["text"].strip()
    # The backtest numbers must be the single published set.
    accuracy_q = next(q for q in qs if "accurate" in q["name"].lower())
    text = accuracy_q["acceptedAnswer"]["text"]
    assert "74.1%" in text and "93%" in text and "0.88" in text


async def _get(url):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(url)


@pytest.mark.asyncio
async def test_robots_txt_served_and_allows_ai_crawlers():
    resp = await _get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "<div id=\"root\">" not in body           # NOT the SPA shell
    for bot in ("GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended"):
        assert bot in body
    assert "Allow: /" in body
    assert "Disallow: /api/" in body
    assert "Disallow: /dashboard" in body
    assert "Sitemap: https://rookff.com/sitemap.xml" in body


@pytest.mark.asyncio
async def test_sitemap_is_valid_xml_with_public_urls():
    resp = await _get("/sitemap.xml")
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"]
    import xml.etree.ElementTree as ET

    root = ET.fromstring(resp.text)  # raises on malformed XML
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locs = {u.find(f"{ns}loc").text for u in root.findall(f"{ns}url")}
    assert "https://rookff.com/" in locs
    assert "https://rookff.com/pricing" in locs
    assert "https://rookff.com/privacy" in locs


@pytest.mark.asyncio
async def test_llms_txt_served_with_prices_from_user_py():
    resp = await _get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "<div id=\"root\">" not in body
    assert "# Rook" in body
    # Prices derived from user.py.
    assert f"${TIER_LIMITS['standard']['price_monthly_usd']}/month" in body
    assert f"${TIER_LIMITS['standard']['price_season_usd']}/season" in body
    assert f"${TIER_LIMITS['pro']['price_monthly_usd']}/month" in body
    # The defensible defense/VOR figure — Vikings, not Broncos.
    assert "Vikings" in body and "31.5" in body and "2.4" in body
    assert "Broncos" not in body
