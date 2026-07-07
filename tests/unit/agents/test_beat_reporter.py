"""
tests/unit/agents/test_beat_reporter.py

All required named test cases from stage-06-to-10.md (Stage 8).
Additional coverage tests to reach 80%+ on beat_reporter.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.beat_reporter import (
    BeatReporterAgent,
    _article_entity_id,
    _fetch_all_feeds,
    _get_agent,
    _load_player_map,
    _load_seen_articles,
    _map_key,
    _resolve_player,
    _update_injury_recovery,
    _update_player_notes,
    _update_player_team,
    _write_signal,
    run,
    setup_scheduler,
    SIGNAL_TYPES,
    ESPN_NFL_FEED,
    ROTOWIRE_FEED,
    NFL_COM_FEED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_article(
    title: str = "Patrick Mahomes limited in practice",
    summary: str = "Mahomes was limited Wednesday with a knee issue.",
    source: str = ESPN_NFL_FEED,
    url: str = "https://example.com/article",
) -> dict:
    return {"title": title, "summary": summary, "source": source, "url": url}


def _make_player(
    name: str = "Patrick Mahomes",
    team: str = "KC",
    player_id: str = "player-uuid-1",
) -> MagicMock:
    p = MagicMock()
    p.id = player_id
    p.name = name
    p.team_abbr = team
    return p


def _make_signal(
    player_name: str = "Patrick Mahomes",
    player_team: str = "KC",
    signal_type: str = "injury_flag",
    confidence: str = "high",
    summary: str = "Mahomes limited with knee issue.",
) -> dict:
    return {
        "player_name": player_name,
        "player_team": player_team,
        "signal_type": signal_type,
        "confidence":  confidence,
        "summary":     summary,
    }


def _make_feed_entry(
    title: str = "Patrick Mahomes limited",
    summary: str = "Short snippet.",
    link: str = "https://espn.com/nfl/1",
) -> MagicMock:
    entry = MagicMock()
    entry.get = lambda key, default=None: {
        "title":   title,
        "summary": summary,
        "link":    link,
    }.get(key, default)
    return entry


# ---------------------------------------------------------------------------
# Required test cases — Stage 8 spec
# ---------------------------------------------------------------------------

def test_rss_feed_parsed_correctly():
    """RSS feed entries must be extracted into article dicts with title, source, url."""
    mock_entry = _make_feed_entry(
        title   = "Davante Adams signs with new team",
        summary = "Adams agreed to a 1-year deal.",
        link    = "https://espn.com/davante",
    )
    mock_feed = MagicMock()
    mock_feed.entries = [mock_entry]

    with patch("feedparser.parse", return_value=mock_feed):
        articles = _fetch_all_feeds([ESPN_NFL_FEED])

    assert len(articles) == 1
    assert articles[0]["title"]  == "Davante Adams signs with new team"
    assert articles[0]["source"] == ESPN_NFL_FEED
    assert articles[0]["url"]    == "https://espn.com/davante"
    assert "summary" in articles[0]


def test_player_name_matched_to_db_record():
    """Player name extracted by model must be resolved to a DB player_id."""
    mock_player = _make_player("Patrick Mahomes", "KC", "uuid-kc-qb")
    player_map  = {"mahomes": [mock_player]}

    player_id = _resolve_player("Patrick Mahomes", "KC", player_map)
    assert player_id == "uuid-kc-qb"


async def test_signal_written_to_beat_reporter_signals_table():
    """A valid signal must be written to the beat_reporter_signals table."""
    signal  = _make_signal()
    article = _make_article()

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        result = await _write_signal(signal, article, "player-uuid-1")

    assert result is True
    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()


async def test_player_notes_updated_after_signal():
    """After a signal is written, the player's notes field must be updated."""
    signal = _make_signal(summary="Mahomes limited with knee issue.")

    mock_player = MagicMock()
    mock_player.notes = "Prior note."

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_player)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_notes("player-uuid-1", signal)

    assert "[Beat]" in mock_player.notes
    assert "Mahomes limited with knee issue." in mock_player.notes
    mock_session.commit.assert_called_once()


def test_scheduler_job_registered():
    """setup_scheduler() must register a job with id='beat_reporter_daily'."""
    agent     = BeatReporterAgent(dry_run=True)
    scheduler = setup_scheduler(agent=agent)
    # Inspect jobs without starting the scheduler
    job_ids = [job.id for job in scheduler.get_jobs()]
    assert "beat_reporter_daily" in job_ids


async def test_duplicate_signals_not_written_twice():
    """Articles already in beat_reporter_signals must not trigger a second API call."""
    agent   = BeatReporterAgent(dry_run=False)
    article = _make_article(title="Mahomes limited", source=ESPN_NFL_FEED)

    seen = {(ESPN_NFL_FEED, "Mahomes limited")}  # already in DB

    with patch("backend.agents.beat_reporter._fetch_all_feeds", return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles",  new_callable=AsyncMock, return_value=seen), \
         patch("backend.agents.beat_reporter._load_player_map",     new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "call_once", new_callable=AsyncMock)   as mock_call:

        result = await agent.run()

    mock_call.assert_not_called()
    assert result == 0


# ---------------------------------------------------------------------------
# _article_entity_id
# ---------------------------------------------------------------------------

def test_article_entity_id_is_deterministic():
    article = _make_article()
    assert _article_entity_id(article) == _article_entity_id(article)


def test_article_entity_id_differs_across_articles():
    a1 = _make_article(title="Article A")
    a2 = _make_article(title="Article B")
    assert _article_entity_id(a1) != _article_entity_id(a2)


def test_article_entity_id_is_32_chars():
    assert len(_article_entity_id(_make_article())) == 32


# ---------------------------------------------------------------------------
# _fetch_all_feeds
# ---------------------------------------------------------------------------

def test_fetch_all_feeds_multiple_feeds():
    entry1 = _make_feed_entry("ESPN headline")
    entry2 = _make_feed_entry("Rotowire headline")

    feed1 = MagicMock(); feed1.entries = [entry1]
    feed2 = MagicMock(); feed2.entries = [entry2]

    with patch("feedparser.parse", side_effect=[feed1, feed2]):
        articles = _fetch_all_feeds([ESPN_NFL_FEED, ROTOWIRE_FEED])

    assert len(articles) == 2


def test_fetch_all_feeds_skips_empty_title():
    empty_entry = _make_feed_entry(title="")
    feed = MagicMock(); feed.entries = [empty_entry]

    with patch("feedparser.parse", return_value=feed):
        articles = _fetch_all_feeds([ESPN_NFL_FEED])

    assert articles == []


def test_fetch_all_feeds_handles_feed_failure():
    """Failed feeds are logged and skipped — run does not crash."""
    with patch("feedparser.parse", side_effect=Exception("network error")):
        articles = _fetch_all_feeds([ESPN_NFL_FEED])

    assert articles == []


def test_fetch_all_feeds_truncates_long_summary():
    long_summary = "x" * 1000
    entry = _make_feed_entry(summary=long_summary)
    feed  = MagicMock(); feed.entries = [entry]

    with patch("feedparser.parse", return_value=feed):
        articles = _fetch_all_feeds([ESPN_NFL_FEED])

    assert len(articles[0]["summary"]) <= 500


def test_fetch_all_feeds_empty_entries():
    feed = MagicMock(); feed.entries = []
    with patch("feedparser.parse", return_value=feed):
        articles = _fetch_all_feeds([ESPN_NFL_FEED])
    assert articles == []


# ---------------------------------------------------------------------------
# _resolve_player
# ---------------------------------------------------------------------------

def test_resolve_player_exact_last_name():
    player = _make_player("Justin Jefferson", "MIN", "uuid-min")
    player_map = {"jefferson": [player]}
    assert _resolve_player("Justin Jefferson", "MIN", player_map) == "uuid-min"


def test_resolve_player_no_name_returns_none():
    assert _resolve_player(None, "KC", {}) is None


def test_resolve_player_no_match_returns_none():
    assert _resolve_player("Unknown Person", "KC", {}) is None


def test_resolve_player_disambiguates_by_team():
    p_sf  = _make_player("Deebo Samuel", "SF",  "uuid-sf")
    p_sf.team_abbr  = "SF"
    p_min = _make_player("Sam X", "MIN", "uuid-min")
    p_min.team_abbr = "MIN"
    player_map = {"samuel": [p_sf, p_min]}

    result = _resolve_player("Deebo Samuel", "SF", player_map)
    assert result == "uuid-sf"


def test_resolve_player_single_candidate_no_team_needed():
    player = _make_player("Travis Kelce", "KC", "uuid-kc")
    player_map = {"kelce": [player]}
    result = _resolve_player("Travis Kelce", None, player_map)
    assert result == "uuid-kc"


def test_resolve_player_falls_back_to_first_when_team_mismatch():
    p1 = _make_player("Mike Evans", "TB", "uuid-tb"); p1.team_abbr = "TB"
    p2 = _make_player("Mike X",    "SF", "uuid-sf"); p2.team_abbr = "SF"
    player_map = {"evans": [p1, p2]}
    # team not provided — falls back to first candidate
    result = _resolve_player("Mike Evans", None, player_map)
    assert result == "uuid-tb"


# ---------------------------------------------------------------------------
# Attribution regression fixtures (Threads 2+4) — the two real mis-attributions,
# each exercising a different half of the bug, verified against real DB records.
# ---------------------------------------------------------------------------

def _mk(name, team, pid, sleeper_id, tier=None, rbc=None):
    """Mock player with the fields the ranking reads (all comparable)."""
    p = MagicMock()
    p.id = pid
    p.name = name
    p.team_abbr = team
    p.sleeper_id = sleeper_id
    p.tier = tier
    p.recommended_bid_ceiling = rbc
    p.ai_bid_ceiling = None
    return p


def test_map_key_strips_suffix():
    # The Godwin half: "Chris Godwin Jr." must key under "godwin", not "jr.".
    assert _map_key("Chris Godwin Jr.") == "godwin"
    assert _map_key("Chris Godwin") == "godwin"
    assert _map_key("Marvin Harrison Jr.") == "harrison"
    assert _map_key("Odell Beckham Jr.") == "beckham"
    assert _map_key("Michael Pittman II") == "pittman"
    assert _map_key("") is None


def test_resolve_evans_prefers_synced_prominent_over_stale_null_row():
    # Thread 4 half — the real Mike Evans case. A stale non-synced duplicate
    # ("Omari Evans", sleeper_id=None) whose STALE team happens to match the
    # article must NOT win over the correct anchored Mike Evans (sleeper_id 2216),
    # whose own canonical team is stale ("SF"). First-name mismatch alone already
    # rejects Omari; prominence/anchoring is the belt-and-suspenders.
    stale_omari = _mk("Omari Evans", "TB", "uuid-omari", None, tier=None)
    mike = _mk("Mike Evans", "SF", "uuid-mike", "2216", tier=4, rbc=9)
    player_map = {"evans": [stale_omari, mike]}
    assert _resolve_player("Mike Evans", "TB", player_map) == "uuid-mike"


def test_resolve_godwin_reachable_after_suffix_strip():
    # Thread 2 half — the real Chris Godwin case. With suffix-stripped keying,
    # "Chris Godwin Jr." lands under "godwin" and resolves from the suffix-less
    # article name; the same-surname nobody "Terry Godwin" is rejected on the
    # first-name mismatch, not attributed.
    chris = _mk("Chris Godwin Jr.", "TB", "uuid-chris", "4037", tier=4, rbc=6)
    terry = _mk("Terry Godwin", None, "uuid-terry", "5977", tier=None)
    player_map = {"godwin": [chris, terry]}
    assert _resolve_player("Chris Godwin", "TB", player_map) == "uuid-chris"


def test_resolve_collision_less_prominent_resolves_to_itself():
    # First name disambiguates: the article's LESS-prominent same-surname player
    # must resolve to itself, never to the more prominent one (the A.J. Brown ->
    # Chase Brown class of bug).
    chase = _mk("Chase Brown", "CIN", "uuid-chase", "10222", tier=1, rbc=30)
    aj = _mk("A.J. Brown", "PHI", "uuid-aj", "6794", tier=2, rbc=45)
    player_map = {"brown": [chase, aj]}
    assert _resolve_player("A.J. Brown", "PHI", player_map) == "uuid-aj"
    assert _resolve_player("Chase Brown", "CIN", player_map) == "uuid-chase"


def test_resolve_prominence_breaks_first_initial_tie():
    # When only a first-INITIAL match is available (abbreviated article name),
    # prominence (tier) breaks the tie — more prominent wins.
    mike = _mk("Mike Evans", "SF", "uuid-mike", "2216", tier=4, rbc=9)
    mitchell = _mk("Mitchell Evans", "CAR", "uuid-mitch", "12473", tier=5, rbc=2)
    player_map = {"evans": [mitchell, mike]}
    assert _resolve_player("M. Evans", "TB", player_map) == "uuid-mike"


def test_resolve_last_name_only_collision_refused():
    # Forward-caveat guard: an article name whose FIRST name matches no candidate
    # must NOT be attributed to a startable same-surname player — signal loss is
    # safer than mis-attribution. (This is the injury_flag/depth_chart_change
    # value-relevant path.)
    justin = _mk("Justin Jefferson", "MIN", "uuid-jj", "6794", tier=1, rbc=55)
    player_map = {"jefferson": [justin]}
    assert _resolve_player("Zorpo Jefferson", "MIN", player_map) is None


# ---------------------------------------------------------------------------
# _write_signal
# ---------------------------------------------------------------------------

async def test_write_signal_invalid_signal_type_returns_false():
    signal  = _make_signal(signal_type="unknown_type")
    article = _make_article()
    result  = await _write_signal(signal, article, None)
    assert result is False


async def test_write_signal_null_signal_type_returns_false():
    signal  = {"player_name": "Mahomes", "signal_type": None}
    article = _make_article()
    result  = await _write_signal(signal, article, None)
    assert result is False


async def test_write_signal_sets_correct_fields():
    signal  = _make_signal(signal_type="transaction", confidence="medium")
    article = _make_article(title="Adams released by Raiders", source=ROTOWIRE_FEED)

    captured = []
    mock_session = AsyncMock()
    mock_session.add = MagicMock(side_effect=captured.append)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        result = await _write_signal(signal, article, "player-uuid")

    assert result is True
    rec = captured[0]
    assert rec.signal_type == "transaction"
    assert rec.source      == ROTOWIRE_FEED
    assert rec.raw_text    == "Adams released by Raiders"
    assert rec.confidence  == "medium"


async def test_write_signal_player_id_can_be_none():
    """Signals for unresolved players are still written (player_id=None allowed in schema)."""
    signal  = _make_signal()
    article = _make_article()

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        result = await _write_signal(signal, article, None)

    assert result is True


# ---------------------------------------------------------------------------
# _update_player_notes
# ---------------------------------------------------------------------------

async def test_update_player_notes_appends_to_existing():
    signal = _make_signal(summary="Limited in practice.")

    mock_player       = MagicMock()
    mock_player.notes = "Previous note."

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_player)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_notes("uuid", signal)

    assert "Previous note." in mock_player.notes
    assert "Limited in practice." in mock_player.notes


async def test_update_player_notes_sets_first_note():
    signal = _make_signal(summary="First signal ever.")

    mock_player       = MagicMock()
    mock_player.notes = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_player)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_notes("uuid", signal)

    assert mock_player.notes == "[Beat] First signal ever."


async def test_update_player_notes_no_summary_skips():
    signal = _make_signal(summary="")

    mock_session = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_notes("uuid", signal)

    mock_session.get.assert_not_called()


async def test_update_player_notes_player_not_found():
    signal = _make_signal(summary="Some note.")

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_notes("uuid", signal)

    mock_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# _update_injury_recovery
# ---------------------------------------------------------------------------

async def test_update_injury_recovery_sets_questionable_for_high_confidence():
    signal = _make_signal(signal_type="injury_flag", confidence="high")

    mock_profile = MagicMock()
    r = MagicMock(); r.scalar_one_or_none.return_value = mock_profile
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=r)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_injury_recovery("uuid", signal)

    assert mock_profile.recovery_assessment == "questionable"


async def test_update_injury_recovery_sets_probable_for_low_confidence():
    signal = _make_signal(signal_type="injury_flag", confidence="low")

    mock_profile = MagicMock()
    r = MagicMock(); r.scalar_one_or_none.return_value = mock_profile
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=r)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_injury_recovery("uuid", signal)

    assert mock_profile.recovery_assessment == "probable"


async def test_update_injury_recovery_skips_non_injury_signals():
    signal = _make_signal(signal_type="transaction")

    mock_session = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_injury_recovery("uuid", signal)

    mock_session.execute.assert_not_called()


async def test_update_injury_recovery_no_profile_is_noop():
    signal = _make_signal(signal_type="injury_flag", confidence="high")

    r = MagicMock(); r.scalar_one_or_none.return_value = None
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=r)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_injury_recovery("uuid", signal)

    mock_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# BeatReporterAgent.run()
# ---------------------------------------------------------------------------

async def test_run_returns_zero_when_no_articles():
    agent = BeatReporterAgent(dry_run=False)

    with patch("backend.agents.beat_reporter._fetch_all_feeds",    return_value=[]), \
         patch("backend.agents.beat_reporter._load_seen_articles", new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map",    new_callable=AsyncMock, return_value={}):
        result = await agent.run()

    assert result == 0


async def test_run_skips_articles_without_title():
    agent   = BeatReporterAgent(dry_run=False)
    article = {"title": "", "source": ESPN_NFL_FEED, "summary": "", "url": "http://x.com"}

    with patch("backend.agents.beat_reporter._fetch_all_feeds",    return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles", new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map",    new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "call_once", new_callable=AsyncMock)  as mock_call:
        result = await agent.run()

    mock_call.assert_not_called()
    assert result == 0


async def test_run_calls_api_for_new_article():
    agent   = BeatReporterAgent(dry_run=False)
    article = _make_article(title="CeeDee Lamb standout at OTAs")

    signal_json = '{"player_name": "CeeDee Lamb", "player_team": "DAL", "signal_type": "camp_standout", "confidence": "high", "summary": "Lamb dominant at OTAs."}'

    with patch("backend.agents.beat_reporter._fetch_all_feeds",    return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles", new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map",    new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=signal_json) as mock_call, \
         patch("backend.agents.beat_reporter._write_signal",       new_callable=AsyncMock, return_value=True), \
         patch("backend.agents.beat_reporter._update_player_notes", new_callable=AsyncMock), \
         patch("backend.agents.beat_reporter._update_injury_recovery", new_callable=AsyncMock):
        result = await agent.run()

    mock_call.assert_called_once()
    assert result == 1


async def test_run_dry_run_makes_no_db_writes():
    agent   = BeatReporterAgent(dry_run=True)
    article = _make_article()

    with patch("backend.agents.beat_reporter._fetch_all_feeds",    return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles", new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map",    new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=None), \
         patch("backend.agents.beat_reporter._write_signal",       new_callable=AsyncMock) as mock_write:
        result = await agent.run()

    mock_write.assert_not_called()
    assert result == 0


async def test_run_skips_null_signal_type():
    """Model output with signal_type=null (non-fantasy article) must be skipped."""
    agent   = BeatReporterAgent(dry_run=False)
    article = _make_article(title="NFL announces new stadium rules")

    null_signal = '{"player_name": null, "player_team": null, "signal_type": null, "confidence": null, "summary": null}'

    with patch("backend.agents.beat_reporter._fetch_all_feeds",    return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles", new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map",    new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=null_signal), \
         patch("backend.agents.beat_reporter._write_signal",       new_callable=AsyncMock) as mock_write:
        result = await agent.run()

    mock_write.assert_not_called()
    assert result == 0


async def test_run_updates_injury_recovery_for_injury_flag():
    agent   = BeatReporterAgent(dry_run=False)
    article = _make_article(title="Mahomes limited with knee")

    signal_json = '{"player_name": "Patrick Mahomes", "player_team": "KC", "signal_type": "injury_flag", "confidence": "high", "summary": "Mahomes limited."}'

    mock_player = _make_player("Patrick Mahomes", "KC", "uuid-kc")
    player_map  = {"mahomes": [mock_player]}

    with patch("backend.agents.beat_reporter._fetch_all_feeds",         return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles",      new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map",         new_callable=AsyncMock, return_value=player_map), \
         patch.object(agent, "call_once",                               new_callable=AsyncMock, return_value=signal_json), \
         patch("backend.agents.beat_reporter._write_signal",            new_callable=AsyncMock, return_value=True), \
         patch("backend.agents.beat_reporter._update_player_notes",     new_callable=AsyncMock), \
         patch("backend.agents.beat_reporter._update_injury_recovery",  new_callable=AsyncMock) as mock_injury:
        await agent.run()

    mock_injury.assert_called_once()


# ---------------------------------------------------------------------------
# Constants and signal type coverage
# ---------------------------------------------------------------------------

def test_signal_types_contains_all_required():
    required = {
        "practice_limited",
        "depth_chart_change",
        "injury_flag",
        "camp_standout",
        "transaction",
    }
    assert required.issubset(SIGNAL_TYPES)


def test_feed_urls_are_defined():
    agent = BeatReporterAgent(dry_run=True)
    assert len(agent.FEED_URLS) >= 3
    assert ESPN_NFL_FEED in agent.FEED_URLS
    assert ROTOWIRE_FEED in agent.FEED_URLS
    assert NFL_COM_FEED  in agent.FEED_URLS


# ---------------------------------------------------------------------------
# Module shims
# ---------------------------------------------------------------------------

def test_get_agent_returns_beat_reporter_agent():
    agent = _get_agent(dry_run=True)
    assert isinstance(agent, BeatReporterAgent)
    assert agent.dry_run is True


async def test_run_module_shim_is_async():
    import inspect
    from backend.agents import beat_reporter
    assert inspect.iscoroutinefunction(beat_reporter.run)


# ---------------------------------------------------------------------------
# _update_player_team
# ---------------------------------------------------------------------------

async def test_update_player_team_sets_new_team():
    """Transaction signal should update the player's team_abbr."""
    mock_player = MagicMock()
    mock_player.name = "Davante Adams"
    mock_player.team_abbr = "LV"

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_player)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_team("player-uuid", "NYJ")

    assert mock_player.team_abbr == "NYJ"
    assert mock_player.updated_at is not None
    mock_session.commit.assert_called_once()


async def test_update_player_team_skips_same_team():
    """If player is already on the correct team, no update needed."""
    mock_player = MagicMock()
    mock_player.name = "Patrick Mahomes"
    mock_player.team_abbr = "KC"

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_player)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_team("player-uuid", "KC")

    mock_session.commit.assert_not_called()


async def test_update_player_team_skips_missing_player():
    """If player_id doesn't resolve, no crash, no commit."""
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.beat_reporter.AsyncSessionLocal", return_value=mock_ctx):
        await _update_player_team("bad-uuid", "NYJ")

    mock_session.commit.assert_not_called()


async def test_update_player_team_noop_on_empty_args():
    """Empty player_id or new_team should be a no-op."""
    await _update_player_team("", "NYJ")
    await _update_player_team("uuid", "")
    await _update_player_team(None, "NYJ")


async def test_run_calls_update_player_team_on_transaction():
    """When run() processes a transaction signal, it should call _update_player_team."""
    agent = BeatReporterAgent(dry_run=False)
    article = _make_article(title="Davante Adams signs with Jets")

    signal_json = '{"player_name": "Davante Adams", "player_team": "NYJ", "signal_type": "transaction", "confidence": "high", "summary": "Adams signs with NYJ."}'

    mock_player = _make_player("Davante Adams", "LV", "uuid-lv")
    player_map = {"adams": [mock_player]}

    with patch("backend.agents.beat_reporter._fetch_all_feeds", return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles", new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map", new_callable=AsyncMock, return_value=player_map), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=signal_json), \
         patch("backend.agents.beat_reporter._write_signal", new_callable=AsyncMock, return_value=True), \
         patch("backend.agents.beat_reporter._update_player_notes", new_callable=AsyncMock), \
         patch("backend.agents.beat_reporter._update_injury_recovery", new_callable=AsyncMock), \
         patch("backend.agents.beat_reporter._update_player_team", new_callable=AsyncMock) as mock_team:
        await agent.run()

    mock_team.assert_called_once_with("uuid-lv", "NYJ")


async def test_run_skips_team_update_for_non_transaction():
    """Non-transaction signals should NOT call _update_player_team."""
    agent = BeatReporterAgent(dry_run=False)
    article = _make_article(title="Mahomes limited")

    signal_json = '{"player_name": "Patrick Mahomes", "player_team": "KC", "signal_type": "injury_flag", "confidence": "high", "summary": "Mahomes limited."}'

    mock_player = _make_player("Patrick Mahomes", "KC", "uuid-kc")
    player_map = {"mahomes": [mock_player]}

    with patch("backend.agents.beat_reporter._fetch_all_feeds", return_value=[article]), \
         patch("backend.agents.beat_reporter._load_seen_articles", new_callable=AsyncMock, return_value=set()), \
         patch("backend.agents.beat_reporter._load_player_map", new_callable=AsyncMock, return_value=player_map), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=signal_json), \
         patch("backend.agents.beat_reporter._write_signal", new_callable=AsyncMock, return_value=True), \
         patch("backend.agents.beat_reporter._update_player_notes", new_callable=AsyncMock), \
         patch("backend.agents.beat_reporter._update_injury_recovery", new_callable=AsyncMock), \
         patch("backend.agents.beat_reporter._update_player_team", new_callable=AsyncMock) as mock_team:
        await agent.run()

    mock_team.assert_not_called()
