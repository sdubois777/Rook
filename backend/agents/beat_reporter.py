"""
Agent 6: Beat Reporter Agent

Pre-draft RSS news ingestion. Runs daily via APScheduler.
One Haiku call per article (300 tokens max) to classify signal type and extract player name.

Architecture:
  - Model: Haiku (classification/extraction)
  - Max tokens: 300 per signal (variable total — feed-driven, not team-batched)
  - Pattern: fetch feeds → one call_once() per new article → write to DB
  - APScheduler cron job at 7am daily
  - Duplicate detection: pre-load (source, raw_text) pairs from DB before processing loop
  - Never uses run_agent() (that is for live draft only)

Data sources:
  - ESPN NFL news RSS
  - Rotowire NFL transaction feed
  - NFL.com news RSS
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import ClassVar

import feedparser
from sqlalchemy import select

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU
from backend.database import AsyncSessionLocal
from backend.models.dependency import BeatReporterSignal
from backend.models.player import Player, PlayerInjuryProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal type constants
# ---------------------------------------------------------------------------

SIGNAL_TYPES = frozenset({
    "practice_limited",
    "depth_chart_change",
    "injury_flag",
    "camp_standout",
    "transaction",
})

# ---------------------------------------------------------------------------
# RSS feed URLs
# ---------------------------------------------------------------------------

ESPN_NFL_FEED = "https://www.espn.com/espn/rss/nfl/news"
ROTOWIRE_FEED = "https://www.rotowire.com/rss/news.php?sport=NFL"
NFL_COM_FEED  = "https://www.nfl.com/rss/rsslanding?searchString=news"

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fantasy football news classifier for a pre-draft research system.

Given a news headline and optional snippet, extract:
1. The NFL player name most relevant to fantasy (exactly as it would appear on a roster), or null
2. The NFL team abbreviation (e.g. KC, BUF, LAC) for that player, or null
3. Signal type — exactly one of: practice_limited, depth_chart_change, injury_flag, camp_standout, transaction
4. Confidence: high | medium | low
5. Summary: one concise sentence (max 20 words)

Signal type definitions:
- practice_limited: player reported limited or DNP in practice or on injury report
- depth_chart_change: official or reported change in depth chart position
- injury_flag: any injury mention, return timeline, or health concern
- camp_standout: emerging role signal, training camp praise, increased involvement
- transaction: signing, cut, trade, or contract move

If the article is NOT about a specific fantasy-relevant player, output:
{"player_name": null, "player_team": null, "signal_type": null, "confidence": null, "summary": null}

Output ONLY valid JSON. No explanation, no preamble, no markdown fences.
{"player_name": "First Last" | null, "player_team": "ABBR" | null, "signal_type": "..." | null, "confidence": "high|medium|low" | null, "summary": "..." | null}"""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _article_entity_id(article: dict) -> str:
    """Deterministic 32-char hash of (source + title) — used as cache key."""
    raw = (article.get("source", "") + "|" + article.get("title", "")).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _map_key(name: str) -> str | None:
    """Last-name key for the resolution map, SUFFIX-STRIPPED via the canonical
    _norm_name (so "Chris Godwin Jr." keys under "godwin", not "jr."). Returns the
    lowercased last token, or None if the name has no usable token."""
    from backend.agents.roster_changes import _norm_name
    tokens = _norm_name(name or "").split()
    return tokens[-1] if tokens else None


def _resolve_player(
    name: str | None,
    team: str | None,
    player_map: dict[str, list],
) -> str | None:
    """
    Match a news article name to a DB player_id via the CANONICAL guard.

    News text carries no player id, so this is genuinely name-only — the guarded
    fallback path. Keys by the SUFFIX-STRIPPED last name against a pool that already
    excludes non-synced / non-draftable rows (see _load_player_map), then delegates
    to the ONE shared guard (backend.utils.player_resolver.guarded_name_pick):
    first-name agreement required, last-name-only collision REFUSED (the #217 fix),
    prominence-ranked, loud-warn. The guard lives in exactly one place now, so news
    and roster resolution can never diverge again.
    """
    from backend.utils.player_resolver import guarded_name_pick

    if not name:
        return None
    key = _map_key(name)
    if not key:
        return None
    candidates = player_map.get(key, [])
    if not candidates:
        logger.warning(
            "beat_reporter: no eligible candidate for name=%r team=%r (key=%r) — "
            "signal NOT attributed (pool excludes non-synced/non-draftable rows)",
            name, team, key,
        )
        return None

    best = guarded_name_pick(candidates, name, team=team)
    return str(best.id) if best else None


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _fetch_all_feeds(feed_urls: list[str]) -> list[dict]:
    """
    Fetch all RSS feeds using feedparser (synchronous).
    Returns list of article dicts: {title, summary, url, source}.
    Feed failures are logged and skipped — never crash the run.
    """
    articles: list[dict] = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title   = (entry.get("title")   or "").strip()
                summary = (entry.get("summary")  or "").strip()[:500]
                link    = entry.get("link") or url
                if title:
                    articles.append({
                        "title":   title,
                        "summary": summary,
                        "url":     link,
                        "source":  url,
                    })
        except Exception as exc:
            logger.warning("Failed to fetch feed %s: %s", url, exc)
    return articles


async def _load_seen_articles() -> set[tuple[str, str]]:
    """
    Load (source, raw_text) pairs from beat_reporter_signals.
    Pre-loaded once before the article loop — prevents N+1 duplicate checks.
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(BeatReporterSignal.source, BeatReporterSignal.raw_text)
        )).all()
    return {(r.source or "", r.raw_text or "") for r in rows}


async def _load_player_map() -> dict[str, list]:
    """
    Load the resolution pool into an in-memory last-name map.

    The pool is FILTERED (Threads 2+4): only SYNCED players (sleeper_id present)
    that pass the same draftable_filter every other consumer uses. This keeps the
    stale, non-synced duplicate rows (e.g. "Omari Evans (TB)", sleeper_id=None)
    out of attribution entirely — they were the rows that poisoned resolution
    while being hidden everywhere else. Keys are SUFFIX-STRIPPED (canonical
    _norm_name) so "Chris Godwin Jr." keys under "godwin", not "jr.".

    Excluded rows are counted and logged loudly — never silently dropped.
    """
    from sqlalchemy import and_, func, not_

    from backend.repositories.player_repo import draftable_filter

    eligible = and_(Player.sleeper_id.isnot(None), draftable_filter())
    async with AsyncSessionLocal() as session:
        players = (await session.execute(
            select(Player).where(eligible)
        )).scalars().all()
        excluded = (await session.execute(
            select(func.count()).select_from(Player).where(not_(eligible))
        )).scalar() or 0

    player_map: dict[str, list] = {}
    dropped_no_key = 0
    for p in players:
        key = _map_key(p.name) if p.name else None
        if not key:
            dropped_no_key += 1
            continue
        player_map.setdefault(key, []).append(p)

    logger.warning(
        "beat_reporter resolution pool: %d eligible players (synced + draftable) "
        "across %d last-name keys; %d rows EXCLUDED (non-synced or non-draftable) "
        "and %d skipped (no usable name) — excluded rows are NOT eligible for news "
        "attribution",
        len(players), len(player_map), excluded, dropped_no_key,
    )
    return player_map


async def _write_signal(
    signal: dict,
    article: dict,
    player_id: str | None,
) -> bool:
    """
    Write one BeatReporterSignal record to DB.
    Returns True if written, False if signal_type is missing or invalid.
    """
    signal_type = signal.get("signal_type")
    if not signal_type or signal_type not in SIGNAL_TYPES:
        return False

    async with AsyncSessionLocal() as session:
        rec = BeatReporterSignal(
            player_id   = player_id,
            signal_type = signal_type,
            source      = article.get("source", ""),
            raw_text    = article.get("title", ""),
            article_url = article.get("url"),   # per-article permalink (entry.link)
            confidence  = signal.get("confidence"),
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)

        # Push to live news WebSocket subscribers
        try:
            from backend.websocket.manager import news_ws_manager
            if news_ws_manager.connection_count > 0:
                await news_ws_manager.broadcast({
                    "id": str(rec.id),
                    "signal_type": signal_type,
                    "source": article.get("source", ""),
                    "raw_text": article.get("title", ""),
                    "article_url": article.get("url"),
                    "confidence": signal.get("confidence"),
                    "flagged_at": rec.flagged_at.isoformat() if rec.flagged_at else None,
                    "player_id": str(player_id) if player_id else None,
                    "player_name": signal.get("player_name"),
                })
        except Exception as exc:
            logger.debug("News WS broadcast failed: %s", exc)

    return True


async def _update_player_notes(player_id: str, signal: dict) -> None:
    """Append the signal summary to the player's notes field."""
    summary = (signal.get("summary") or "").strip()
    if not summary:
        return
    async with AsyncSessionLocal() as session:
        player = await session.get(Player, player_id)
        if not player:
            return
        existing = (player.notes or "").strip()
        player.notes = f"{existing}\n[Beat] {summary}".strip() if existing else f"[Beat] {summary}"
        await session.commit()


async def _update_player_team(player_id: str, new_team: str) -> None:
    """Update a player's team_abbr when a transaction signal confirms a team change."""
    if not player_id or not new_team:
        return
    async with AsyncSessionLocal() as session:
        player = await session.get(Player, player_id)
        if not player or player.team_abbr == new_team.upper():
            return
        logger.info(
            "Beat reporter team sync: %s — %s → %s",
            player.name, player.team_abbr, new_team.upper(),
        )
        player.team_abbr = new_team.upper()
        player.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def _update_injury_recovery(player_id: str, signal: dict) -> None:
    """
    For injury_flag signals, update recovery_assessment on PlayerInjuryProfile.
    Feeds into the Injury Risk agent's assessment field per spec.
    """
    if signal.get("signal_type") != "injury_flag":
        return
    confidence  = signal.get("confidence", "low")
    assessment  = {"high": "questionable", "medium": "questionable"}.get(confidence, "probable")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PlayerInjuryProfile).where(PlayerInjuryProfile.player_id == player_id)
        )
        profile = result.scalar_one_or_none()
        if profile:
            profile.recovery_assessment = assessment
            await session.commit()


# ---------------------------------------------------------------------------
# Beat Reporter Agent
# ---------------------------------------------------------------------------

class BeatReporterAgent(BaseAgent):
    """
    Agent 6: Beat Reporter — daily RSS ingestion for pre-draft player news.

    Unlike other pipeline agents, this is NOT batched by team.
    Each new article receives ONE API call (300 tokens max).
    Run via APScheduler daily at 7am or manually via pipeline script.
    """
    AGENT_NAME       = "beat_reporter"
    AGENT_MODEL      = HAIKU
    AGENT_MAX_TOKENS = 300

    FEED_URLS: ClassVar[list[str]] = [
        ESPN_NFL_FEED,
        ROTOWIRE_FEED,
        NFL_COM_FEED,
    ]

    async def run(self) -> int:
        """
        Fetch all RSS feeds, classify each new article, write signals to DB.
        Returns count of new signals written.
        """
        articles = _fetch_all_feeds(self.FEED_URLS)
        logger.info(
            "Beat Reporter: fetched %d articles from %d feed(s)",
            len(articles), len(self.FEED_URLS),
        )

        if not articles:
            return 0

        # Pre-load both caches once — no queries inside the loop
        seen       = await _load_seen_articles()
        player_map = await _load_player_map()

        written = 0
        for article in articles:
            if not article.get("title"):
                continue
            dedup_key = (article.get("source", ""), article.get("title", ""))
            if dedup_key in seen:
                logger.debug("Duplicate skipped: %.60s", article["title"])
                continue

            # ONE API call per new article — call_once() handles caching
            raw = await self.call_once(
                system     = SYSTEM_PROMPT,
                user       = (
                    f"Headline: {article['title']}\n"
                    f"Snippet: {article.get('summary', '')}"
                ),
                input_data = {
                    "title":   article["title"],
                    "summary": article.get("summary", ""),
                },
                entity_id  = _article_entity_id(article),
            )
            if raw is None:
                # dry_run or cached — skip write
                continue

            signal = parse_json_output(raw)
            if not isinstance(signal, dict) or not signal.get("signal_type"):
                continue

            player_id = _resolve_player(
                signal.get("player_name"),
                signal.get("player_team"),
                player_map,
            )

            wrote = await _write_signal(signal, article, player_id)
            if wrote:
                written += 1
                seen.add(dedup_key)   # mark seen so same-run duplicates are skipped

                if player_id:
                    await _update_player_notes(player_id, signal)
                    await _update_injury_recovery(player_id, signal)
                    # Update team_abbr on transaction signals
                    if signal.get("signal_type") == "transaction" and signal.get("player_team"):
                        await _update_player_team(player_id, signal["player_team"])

        logger.info("Beat Reporter: %d new signal(s) written", written)
        return written


# ---------------------------------------------------------------------------
# APScheduler integration
# ---------------------------------------------------------------------------

def setup_scheduler(agent: BeatReporterAgent | None = None):
    """
    Create an AsyncIOScheduler with the beat_reporter_daily cron job at 7am.
    Returns the configured (not yet started) scheduler.
    Caller is responsible for calling scheduler.start() and scheduler.shutdown().

    Usage in FastAPI lifespan:
        scheduler = setup_scheduler()
        scheduler.start()
        ...
        scheduler.shutdown()
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    _agent = agent or BeatReporterAgent(dry_run=False)

    scheduler.add_job(
        _agent.run,
        "cron",
        hour=7,
        id="beat_reporter_daily",
        replace_existing=True,
    )
    return scheduler


# ---------------------------------------------------------------------------
# Module-level convenience shim
# ---------------------------------------------------------------------------

_agent_instance: BeatReporterAgent | None = None


def _get_agent(dry_run: bool = False) -> BeatReporterAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = BeatReporterAgent(dry_run=dry_run)
    return _agent_instance


async def run(dry_run: bool = False) -> int:
    return await _get_agent(dry_run).run()
