"""Backfill beat_reporter_signals.article_url for legacy rows.

The article permalink is now captured at ingestion, but rows written before the
column existed have article_url = NULL (non-clickable). This re-parses the SAME
source feeds the agent uses and fills in the permalink for any legacy row whose
stored title still matches a current feed entry — using the source's OWN link,
never a fabricated one. Rows whose article has aged out of the feed stay NULL
(they simply remain non-clickable). Idempotent; only touches NULL article_url.

Run (PowerShell):
    uv run python scripts/backfill_news_article_urls.py
"""
from __future__ import annotations
import asyncio
import sys


async def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models.dependency import BeatReporterSignal
    from backend.agents.beat_reporter import _fetch_all_feeds, BeatReporterAgent

    # title -> permalink, from the exact feeds the agent ingests.
    articles = _fetch_all_feeds(BeatReporterAgent.FEED_URLS)
    by_title = {a["title"]: a["url"] for a in articles if a.get("title") and a.get("url")}
    print(f"fetched {len(articles)} live feed entries ({len(by_title)} unique titles)")

    filled = scanned = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(BeatReporterSignal).where(BeatReporterSignal.article_url.is_(None))
        )).scalars().all()
        for sig in rows:
            scanned += 1
            url = by_title.get((sig.raw_text or "").strip())
            if url:
                sig.article_url = url
                filled += 1
        await db.commit()

    print(f"scanned {scanned} rows with NULL article_url; backfilled {filled}")
    print(f"({scanned - filled} left NULL — article no longer in the live feed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
