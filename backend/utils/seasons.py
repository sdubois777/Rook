"""
Season year utilities — dynamic calculation, never hardcoded.

All agents must import from here. Never hardcode season years.

NFL season calendar:
  - Regular season: September–January
  - Playoffs + Super Bowl: January–February
  - New league year (free agency): March
  - NFL Draft: April
  - Training camp / preseason: July–August

Logic:
  - If current month >= 3 (March), the current calendar year IS the current NFL season
    (new league year begins in March — free agency, new contracts)
  - If current month < 3 (Jan–Feb), we're in the tail of the previous season
    (playoffs + Super Bowl still in progress)

Usage:
    from backend.utils.seasons import (
        get_current_season,
        get_analysis_seasons,
        get_analysis_year,
    )

    CURRENT_SEASON   = get_current_season()       # The most recently completed season
    ANALYSIS_SEASONS = get_analysis_seasons(3)    # Last N seasons for historical data
    ANALYSIS_YEAR    = get_analysis_year()        # The upcoming draft we're preparing for
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)


def get_current_season() -> int:
    """
    Returns the most recently started NFL season year.

    The NFL new league year begins in March (free agency
    opens, new contracts signed). January and February
    are the only months where the prior year's season
    is still the current one (playoffs + Super Bowl).

    Examples:
      January 2026  → 2025 (playoffs in progress)
      February 2026 → 2025 (Super Bowl month)
      March 2026    → 2026 (new league year begins)
      August 2026   → 2026 (draft prep / training camp)
      December 2026 → 2026 (regular season week 14)
    """
    today = date.today()
    return today.year if today.month >= 3 else today.year - 1


def get_analysis_year() -> int:
    """
    Returns the season year we're building the draft bible for.

    This equals get_current_season() — the new league year begins in March,
    and get_current_season() already advances to the new year at that point.
    From March onward we're preparing for THIS season's draft.

    Examples:
      - Called in May 2026    → 2026  (preparing for 2026 draft)
      - Called in August 2026 → 2026  (draft prep / training camp)
      - Called in January 2026 → 2025 (still in 2025 season)
    """
    return get_current_season()


def get_analysis_seasons(lookback: int = 3) -> list[int]:
    """
    Returns the last N completed seasons for historical data analysis.
    The current season is excluded (may be incomplete or not yet started).

    Args:
        lookback: Number of seasons to include. Default 3.

    Examples (called in March 2026, current season = 2026):
        get_analysis_seasons(3) → [2023, 2024, 2025]
        get_analysis_seasons(5) → [2021, 2022, 2023, 2024, 2025]

    Examples (called in January 2026, current season = 2025):
        get_analysis_seasons(3) → [2022, 2023, 2024]
    """
    current = get_current_season()
    return list(range(current - lookback, current))


def get_previous_season() -> int:
    """Returns the season immediately before the current one."""
    return get_current_season() - 1


# ---------------------------------------------------------------------------
# Current NFL WEEK — the real in-season time anchor (replaces the demo pin as
# the canonical week source). Pure derivation over the already-cached nflverse
# schedule; the value engine's blend weight (min(1, games/5)) and staleness are
# both functions of the current week, so nothing in-season is time-correct
# without this.
# ---------------------------------------------------------------------------
# The last REG week of an NFL season (17-game / 18-week format since 2021).
_LAST_REG_WEEK = 18


def current_week_from_schedule(schedule_df, now: datetime | None = None) -> int:
    """Derive the current/upcoming fantasy week from an nflverse schedule frame.

    PURE (no fetch) so it's unit-testable against fixtures — ``get_current_nfl_week``
    is the thin wrapper that supplies the cached ``fetch_schedules`` frame.

    DEFINITION (explicit + tested): the current fantasy week is the FIRST REG week
    that is not yet COMPLETE — the smallest week that still has a game whose kickoff
    is in the future. This is the week a fantasy manager is acting on:
      * Tue/Wed before a week's Thursday opener → that (UPCOMING) week
      * during the week's games (Thu–Mon) → that (CURRENT, in-progress) week
      * once the week's FINAL game has kicked off → rolls to the next week
    A value tool (not live scoring) is insensitive to the exact roll moment: a
    week's stats only enter the data layer once its games are played, so
    games-played — and therefore the blend — is unchanged whether the roll happens
    Monday night or Tuesday.

    SENTINELS:
      * OFFSEASON / pre-season — ``now`` is before the season's FIRST kickoff → 0.
        The blend treats week 0 as games=0 → prior-only, and "week 0" is
        behaviorally identical to "week 1 before any game is played", so the
        offseason↔opener boundary is safe either way.
      * SEASON COMPLETE — every REG game has kicked off → the last REG week
        (``_LAST_REG_WEEK`` = 18). Never returns 19+.

    TIMEZONE: nflverse ``gametime`` is US Eastern. Kickoffs are localized to
    ``America/New_York`` (pandas handles EDT/EST per date — a September game is
    EDT, a January game EST) and compared in UTC against ``now`` (default: current
    UTC time). Loud-warns (never silently returns a wrong week) when the frame is
    empty / missing columns / has unparseable rows.
    """
    import pandas as pd

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if schedule_df is None or getattr(schedule_df, "empty", True):
        logger.warning("current_week: schedule frame empty/None — returning offseason week 0")
        return 0
    needed = {"game_type", "week", "gameday", "gametime"}
    missing = needed - set(schedule_df.columns)
    if missing:
        logger.warning("current_week: schedule missing columns %s — returning week 0", missing)
        return 0

    reg = schedule_df[schedule_df["game_type"] == "REG"].copy()
    if reg.empty:
        logger.warning("current_week: no REG games in schedule — returning week 0")
        return 0

    # Build tz-aware kickoff datetimes: gameday (YYYY-MM-DD) + gametime (HH:MM ET).
    # A missing time defaults to 13:00 ET (the standard early window) so the game
    # still counts toward its week's completeness rather than being dropped.
    gt = (reg["gametime"].astype(str).str.slice(0, 5)
          .replace({"": "13:00", "nan": "13:00", "NaT": "13:00", "None": "13:00"}))
    gd = reg["gameday"].astype(str).str.slice(0, 10)
    kick = pd.to_datetime(gd + " " + gt, format="%Y-%m-%d %H:%M", errors="coerce")
    kick = kick.dt.tz_localize(
        "America/New_York", nonexistent="shift_forward", ambiguous=True,
    ).dt.tz_convert("UTC")
    reg = reg.assign(_kick=kick)

    bad = int(reg["_kick"].isna().sum())
    if bad:
        logger.warning("current_week: %d REG game(s) had an unparseable date/time — skipped", bad)
    reg = reg.dropna(subset=["_kick"])
    if reg.empty:
        logger.warning("current_week: no parseable REG kickoffs — returning week 0")
        return 0

    season_start = reg["_kick"].min()
    if now < season_start:
        return 0  # offseason / pre-season — no REG game has kicked off yet

    # A week is COMPLETE once its LAST game has kicked off; the current week is the
    # first week not yet complete (min week whose max kickoff is still in the future).
    per_week_last_kick = reg.groupby("week")["_kick"].max()
    future = per_week_last_kick[per_week_last_kick > now]
    if len(future):
        return int(future.index.min())
    return int(per_week_last_kick.index.max())  # season complete → last REG week


def get_current_nfl_week(season: int | None = None, now: datetime | None = None) -> int:
    """The current/upcoming NFL fantasy week for ``season`` (default: the current
    season), derived from the already-cached nflverse schedule.

    This is the CANONICAL real-season week source. The trade/waiver/matchup demo
    keeps its own explicit ``DEMO_CURRENT_WEEK`` pin (so we can seed any test week),
    but a real league's ``LeagueState.week`` derives from here — not from the pin.

    Safe: any failure to load the schedule loud-warns and returns 0 (offseason
    sentinel → the blend falls back to prior-only rather than crashing a request).
    """
    if season is None:
        season = get_current_season()
    from backend.integrations.nfl_data import fetch_schedules
    try:
        schedule = fetch_schedules(season)
    except Exception as exc:  # network/parse failure — never crash the caller
        logger.warning(
            "current_week: could not load the %s schedule (%s) — returning week 0", season, exc,
        )
        return 0
    return current_week_from_schedule(schedule, now=now)


def _default_season_has_data(season: int) -> bool:
    """Real data probe for :func:`latest_season_with_data`: does ``season`` actually
    have game data yet?

    Derived from the nflverse SCHEDULE (``get_current_nfl_week`` > 0 ⇔ at least one
    REG game has kicked off), NOT from a stat-table read. Deliberate: the stat caches
    (NGS/weekly) cache EMPTY results for a not-yet-started season, which would
    stale-pin the probe to False and defeat the auto-advance-to-next-season
    requirement. The schedule is static (only "now" moves against fixed kickoffs), so
    this flips to True on its own the moment a new season's games begin — no cache
    bust, no hardcode. Loud-warn/degrade is handled inside ``get_current_nfl_week``.
    """
    return get_current_nfl_week(season) > 0


def latest_season_with_data(has_data=None, *, lookback: int = 4) -> int:
    """The most-recent NFL season that ACTUALLY has ingested data — the single
    source of truth for "which season do the completed-data metrics read".

    Every Teams-page metric (QB value/EPA, scheme, run-block, pass-protection)
    resolves its season HERE, so they provably agree instead of each improvising.

    Why this exists (the bug it fixes): ``get_current_season() - 1`` is a *calendar*
    guess. In Jan/Feb ``get_current_season()`` is already the prior year (playoffs in
    progress), so ``- 1`` skips a whole year of available data — that is the
    QB-reads-2024-while-the-line-reads-2025 divergence. This probes the real data
    instead, returning the same correct season year-round.

    ``has_data(season) -> bool`` is the data probe. DEFAULT: the schedule-based
    :func:`_default_season_has_data`. INJECTABLE so a caller can probe its own source
    (e.g. ``team_systems`` passes a warehouse probe) and so tests never touch the
    network.

    Probes from ``get_current_season()`` down ``lookback`` seasons and returns the
    newest that has data. If NONE resolves (data source unavailable), loud-warns and
    returns ``get_current_season()`` as a last resort — the caller then reads an empty
    source and degrades gracefully rather than crashing.
    """
    ceiling = get_current_season()
    if has_data is None:
        has_data = _default_season_has_data
    for season in range(ceiling, ceiling - lookback - 1, -1):
        try:
            if has_data(season):
                return season
        except Exception as exc:  # a probe failure must not sink the whole resolve
            logger.warning(
                "latest_season_with_data: data probe failed for %s (%s) — skipping",
                season, exc,
            )
    logger.warning(
        "latest_season_with_data: no ingested data found in seasons %d..%d — falling "
        "back to calendar season %d (data source may be unavailable)",
        ceiling - lookback, ceiling, ceiling,
    )
    return ceiling


def get_fantasypros_auction_year() -> tuple[int, bool]:
    """
    Determine which year's FantasyPros auction data to pull.

    FantasyPros DraftWizard always returns current projections
    regardless of the year URL parameter. The year is therefore
    always get_current_season() and is_current_season is always True.

    Returns:
        (year, is_current_season)

    Examples (called in May 2026):    → (2026, True)
    Examples (called in August 2026): → (2026, True)
    """
    return get_current_season(), True


async def get_best_available_auction_year(
    scraper_fn,
    format: str = "ppr",
) -> tuple[list, int, bool]:
    """
    Scrape FantasyPros DraftWizard auction values.

    DraftWizard always returns current-season projections regardless
    of the year URL parameter, so the year is always get_current_season().

    Args:
        scraper_fn: async function(format, year) → list of player values
        format: scoring format string

    Returns:
        (values, year_used, is_current_season)

    Minimum viable result: 100+ players.
    """
    year, is_current = get_fantasypros_auction_year()

    values = await scraper_fn(format, year)
    if len(values) >= 100:
        logger.info(
            "FantasyPros: fetched %d season data (%d players)",
            year, len(values),
        )
    else:
        logger.warning(
            "FantasyPros: only %d players returned for %d season",
            len(values), year,
        )
    return values, year, is_current


def get_player_seasons_for_baseline(
    nfl_seasons_played: int | None,
    target_clean: int = 4,
) -> list[int]:
    """
    Returns candidate seasons to load for a player's historical baseline.
    Loads enough seasons to yield target_clean clean seasons after injury
    exclusion, capped by career length.

    Always returns seasons in ascending order.
    The caller loads stats for all returned seasons and passes them to
    _compute_weighted_baseline() which handles injury exclusion and weighting.

    Args:
        nfl_seasons_played: career length from DB. None treated as 1 (rookie).
        target_clean: target number of clean seasons. Default 4.

    Examples (called May 2026, current=2026):
      CMC (9 seasons): returns [2020, 2021, 2022, 2023, 2024, 2025]
      2-year player:   returns [2024, 2025]
      Rookie (1):      returns [2025]
    """
    current = get_current_season()
    most_recent_completed = current - 1

    career = max(1, nfl_seasons_played or 1)

    # Target clean seasons, capped by career
    target = min(career, target_clean)

    # Load target + 2 extra as buffer for injury exclusions
    # A player with 2 injury years still gets target clean
    buffer = 2
    max_load = min(career, target + buffer)

    # Build candidate list from most recent going back
    candidates = [
        most_recent_completed - i
        for i in range(max_load)
    ]

    # Return ascending order (oldest first)
    return sorted(candidates)


def get_draft_prep_window() -> dict[str, int]:
    """
    Returns a dict with all season year constants needed by pipeline agents.
    Convenience function to get everything at once.

    Usage:
        window = get_draft_prep_window()
        current    = window["current_season"]
        upcoming   = window["analysis_year"]
        historical = window["analysis_seasons"]  # list[int]
    """
    return {
        "current_season": get_current_season(),
        "previous_season": get_previous_season(),
        "analysis_year": get_analysis_year(),
        "analysis_seasons": get_analysis_seasons(3),
    }
