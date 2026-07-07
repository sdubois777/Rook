"""Per-league roster-slot configuration — canonical model, platform adapters,
and the whole-league fallback guard (T3 of #6 / K/DEF).

The draft/lineup engines run off a hardcoded default lineup. This module lets a
LINKED league's real starting-lineup config override that default, ADDITIVELY:
when a league's normalized slots are present they win; when absent (null), every
consumer keeps using the byte-unchanged default (the #204/#205/#206 K/DEF
standard-lineup path).

Three platform representations normalize onto ONE canonical model:
  - Sleeper: the draft-frame `settings` dict (`slots_qb`, ..., `slots_flex`,
    `slots_k`, `slots_def`, `rounds`) — live-confirmed. Bench is DERIVED
    (rounds − Σ starters), it is not an explicit key.
  - ESPN: the ordered slot-label list read off the resolver's roster template
    (`div[title="Position"]`), e.g. ["QB","RB","RB","WR","WR","TE","FLEX",
    "D/ST","K","BE",...] — verified from real auction + snake fixtures.
  - Yahoo: the ordered slot-token list read off the "YOUR TEAM (n/15)" panel,
    each badge's child <span> letters CONCATENATED (a flex badge is
    <span>W</span><span>R</span><span>T</span> → "WRT"; reading one span misreads
    it as a phantom "W" and corrupts the WR count) — verified from a real
    pre-draft capture.

GUARD (critical): on ANY unrecognized slot token, normalize LOUD-WARNS with the
exact real token + platform + league and returns None — the caller then falls
back to defaults for the WHOLE league. A half-read lineup looks valid and is more
dangerous than a clean fallback, so partial parses are never returned. Tokens we
KNOW but don't model (IDP) map to the UNSUPPORTED bucket instead of failing — the
offense/K/DEF slots stay usable; UNSUPPORTED is never valued.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Canonical slot types. UNSUPPORTED is the documented catch-all for known-but-
# unmodeled slots (IDP) — present so an IDP league still parses its offense slots.
CANONICAL_SLOTS = ("QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF", "BENCH", "IR")
UNSUPPORTED = "UNSUPPORTED"

# Fixed flex eligibility (NOT per-league) — drives roster-need math downstream.
FLEX_ELIGIBLE: dict[str, tuple[str, ...]] = {
    "FLEX": ("RB", "WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}

# ---- per-platform raw-token → canonical maps ------------------------------
# Anything NOT in a platform's map is UNRECOGNIZED → guard fires (whole-league
# fallback). Known-but-unmodeled (IDP) maps to UNSUPPORTED, which is kept.

_SLEEPER_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "FLEX": "FLEX", "SUPER_FLEX": "SUPER_FLEX",
    "K": "K", "DEF": "DEF", "BN": "BENCH", "IR": "IR",
    # known-but-unmodeled
    "IDP_FLEX": UNSUPPORTED, "DL": UNSUPPORTED, "LB": UNSUPPORTED,
    "DB": UNSUPPORTED, "IDP": UNSUPPORTED, "TAXI": UNSUPPORTED,
    "REC_FLEX": "FLEX",  # RB/WR/TE receiving flex → FLEX
}

_ESPN_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "FLEX": "FLEX", "OP": "SUPER_FLEX",   # OP = "Offensive Player" = superflex (PRESUMED — no capture)
    "K": "K", "D/ST": "DEF", "DST": "DEF",
    "BE": "BENCH", "BENCH": "BENCH", "IR": "IR",
    # known-but-unmodeled IDP
    "DL": UNSUPPORTED, "LB": UNSUPPORTED, "DB": UNSUPPORTED,
    "DP": UNSUPPORTED, "CB": UNSUPPORTED, "S": UNSUPPORTED, "DE": UNSUPPORTED, "DT": UNSUPPORTED,
}

_YAHOO_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "WRT": "FLEX", "W/R/T": "FLEX",             # flex (concatenated badge spans / filter value)
    "QWRT": "SUPER_FLEX", "Q/W/R/T": "SUPER_FLEX",  # superflex (PRESUMED — no capture)
    "K": "K", "DEF": "DEF", "BN": "BENCH", "IR": "IR",
    "WR/RB": "FLEX", "RB/WR": "FLEX",           # some Yahoo flex spellings
}

_PLATFORM_MAPS = {"sleeper": _SLEEPER_MAP, "espn": _ESPN_MAP, "yahoo": _YAHOO_MAP}


def resolve_roster_slots(synced: dict | None, live: dict | None = None) -> dict | None:
    """Precedence contract (encoded now, even though the draft-room transport is
    deferred): a SYNCED config is AUTHORITATIVE — the live draft-room parse only
    FILLS a null (never overrides a synced value). Absence of both → None → the
    consumers fall back to the default lineup. A re-sync overwrites via the sync
    path itself (idempotent); this only governs sync-vs-live at read/merge time.

    The future draft-room transport MUST route through this (or its rule) before
    persisting a live parse: `resolve_roster_slots(existing_synced, live_parse)`.
    """
    return synced if synced else (live or None)


def normalize(raw_tokens: list[str], *, platform: str, league: str = "?") -> dict[str, int] | None:
    """Ordered raw slot tokens → canonical {slot_type: count}, or None on any
    unrecognized token (whole-league fallback). Known-but-unmodeled tokens (IDP)
    fold into the UNSUPPORTED bucket. `platform` selects the token map."""
    token_map = _PLATFORM_MAPS.get(platform)
    if token_map is None:
        logger.warning("roster_slots: unknown platform %r (league %s) — falling back to defaults", platform, league)
        return None

    counts: dict[str, int] = {}
    for raw in raw_tokens:
        tok = (raw or "").strip().upper()
        canon = token_map.get(tok)
        if canon is None:
            # UNRECOGNIZED — loud-warn with the exact token; whole-league fallback.
            logger.warning(
                "roster_slots: UNRECOGNIZED slot token %r on %s league %s — "
                "falling back to default lineup for the WHOLE league (no partial parse)",
                raw, platform, league,
            )
            return None
        counts[canon] = counts.get(canon, 0) + 1
    return counts or None


# ---- platform adapters -----------------------------------------------------

def slots_from_sleeper(settings: dict, *, league: str = "?") -> dict[str, int] | None:
    """Sleeper draft-frame `settings` dict → canonical counts. Bench is DERIVED
    (rounds − Σ starter slots), not an explicit key. Expands the discrete
    `slots_*` keys into a token list so it shares the one normalizer + guard."""
    if not isinstance(settings, dict):
        return None
    # discrete slots_<pos> keys → repeated tokens
    tokens: list[str] = []
    starter_total = 0
    for key, val in settings.items():
        if not isinstance(key, str) or not key.startswith("slots_"):
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        token = key[len("slots_"):].upper()  # slots_qb → QB, slots_super_flex → SUPER_FLEX
        tokens.extend([token] * n)
        starter_total += n
    if not tokens:
        return None
    counts = normalize(tokens, platform="sleeper", league=league)
    if counts is None:
        return None
    # Derive bench from rounds − starters (starters = every non-bench slot, incl.
    # UNSUPPORTED/IR which still occupy a drafted round).
    rounds = settings.get("rounds")
    try:
        rounds = int(rounds)
    except (TypeError, ValueError):
        rounds = None
    if rounds is not None:
        bench = rounds - starter_total
        if bench > 0:
            counts["BENCH"] = counts.get("BENCH", 0) + bench
    return counts


def slots_from_espn(slot_tokens: list[str], *, league: str = "?") -> dict[str, int] | None:
    """ESPN roster-template slot labels (div[title='Position']) → canonical counts."""
    return normalize(slot_tokens or [], platform="espn", league=league)


def slots_from_yahoo(slot_tokens: list[str], *, league: str = "?", total_check: int | None = None) -> dict[str, int] | None:
    """Yahoo YOUR-TEAM badge tokens (concatenated span letters) → canonical counts.
    `total_check` = the "n/15" header total; a Σ(slots) mismatch is a corrupt read
    → loud-warn + whole-league fallback (never a partial/mis-split lineup)."""
    counts = normalize(slot_tokens or [], platform="yahoo", league=league)
    if counts is None:
        return None
    if total_check is not None and sum(counts.values()) != total_check:
        logger.warning(
            "roster_slots: Yahoo league %s slot total %d != header %d — corrupt "
            "read, falling back to default lineup for the WHOLE league",
            league, sum(counts.values()), total_check,
        )
        return None
    return counts


# ===========================================================================
# LEAGUE-SETTINGS adapters (the SYNC path) — league already known, no draft-token
# resolution. Reuse the same normalizer + guard; only the pre-normalization shape
# differs from the #208 draft-room adapters above.
# ===========================================================================

def slots_from_sleeper_league(roster_positions, *, league: str = "?") -> dict[str, int] | None:
    """Sleeper `/v1/league/{id}.roster_positions` — an ORDERED ARRAY of slot
    strings (['QB','RB','RB','WR','WR','TE','FLEX','FLEX','K','BN',...]) that IS
    already the token list. Bench is the EXPLICIT 'BN' count here (contrast the
    #208 draft-frame adapter, which derives bench from rounds−Σstarters). VERIFIED
    LIVE against a real league in recon."""
    if not isinstance(roster_positions, (list, tuple)) or not roster_positions:
        return None
    return normalize(list(roster_positions), platform="sleeper", league=league)


def slots_from_yahoo_roster_positions(roster_positions, *, league: str = "?") -> dict[str, int] | None:
    """Yahoo `settings.roster_positions` from the league-SETTINGS response.

    PRESUMED nesting `[{roster_position: {position, count}}, ...]` — UNCONFIRMED
    (the repo mock omits it). Handles both the nested shape AND a flatter
    `[{position, count}]` variant defensively; an unknown position STRING trips
    the normalizer guard (loud-warn + whole-league fallback), so a wrong shape
    FAILS SAFE. Expands position × count → the token list → normalize(yahoo).

    >>> REAL-SAMPLE ASSERTION STUB: once a real get_league_settings response is
        captured, add a fixture test asserting this parses it to the right counts.
    """
    if not isinstance(roster_positions, (list, tuple)) or not roster_positions:
        return None
    tokens: list[str] = []
    for entry in roster_positions:
        if not isinstance(entry, dict):
            logger.warning("roster_slots: Yahoo league %s roster_positions entry not a dict %r "
                           "— whole-league fallback", league, entry)
            return None
        rp = entry.get("roster_position", entry)  # nested or flat
        if not isinstance(rp, dict):
            logger.warning("roster_slots: Yahoo league %s roster_position not a dict %r "
                           "— whole-league fallback", league, rp)
            return None
        pos = rp.get("position")
        try:
            count = int(rp.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        if not pos or count <= 0:
            continue
        tokens.extend([str(pos)] * count)
    if not tokens:
        return None
    return normalize(tokens, platform="yahoo", league=league)


# ESPN LINEUP-SLOT id enum (NOT _ESPN_POS, the player-position enum!). PRESUMED
# from convention — UNCONFIRMED until a real mSettings response is captured. Maps
# id → CANONICAL directly (ids are not self-describing, so we never route them
# through the label map). ANY id not listed here is unknown → whole-league
# fallback (never best-guessed), because a wrong numeric mapping would emit a
# VALID-but-wrong token that would silently corrupt roster needs.
_ESPN_LINEUP_SLOT_ID: dict[int, str] = {
    0: "QB", 2: "RB", 4: "WR", 6: "TE",
    23: "FLEX", 7: "SUPER_FLEX",   # 7 = OP (offensive player / superflex)
    16: "DEF",                     # D/ST
    17: "K",
    20: "BENCH", 21: "IR",
}


def slots_from_espn_lineup_slots(lineup_slot_counts, *, expected_size: int | None = None, league: str = "?") -> dict[str, int] | None:
    """ESPN `settings.rosterSettings.lineupSlotCounts` = {slot_id: count}.

    DEFENSIVE + SAMPLE-GATED. Numeric ids are NOT self-describing, so a wrong
    id→slot mapping would emit a valid-but-wrong token that does NOT trip the
    string guard. Therefore:
      (a) map ONLY the ids in _ESPN_LINEUP_SLOT_ID; ANY other id → loud-warn +
          whole-league fallback (never a best guess);
      (b) if `expected_size` is given, Σ(counts) must equal it → else fallback.
    Until a real mSettings confirms the enum, ESPN leagues are EXPECTED to fall
    back to defaults (safe) — this code lands now; the sample activates it.
    """
    if not isinstance(lineup_slot_counts, dict) or not lineup_slot_counts:
        return None
    counts: dict[str, int] = {}
    for raw_id, raw_cnt in lineup_slot_counts.items():
        try:
            sid, cnt = int(raw_id), int(raw_cnt)
        except (TypeError, ValueError):
            logger.warning("roster_slots: ESPN league %s non-numeric lineupSlot entry %r=%r "
                           "— whole-league fallback", league, raw_id, raw_cnt)
            return None
        if cnt <= 0:
            continue
        canon = _ESPN_LINEUP_SLOT_ID.get(sid)
        if canon is None:
            logger.warning("roster_slots: ESPN league %s UNKNOWN lineup slot id %d — "
                           "enum unconfirmed, falling back to default lineup for the "
                           "WHOLE league (id is not self-describing, never guessed)",
                           league, sid)
            return None
        counts[canon] = counts.get(canon, 0) + cnt
    if not counts:
        return None
    if expected_size is not None and sum(counts.values()) != expected_size:
        logger.warning("roster_slots: ESPN league %s slot total %d != expected roster "
                       "size %d — whole-league fallback", league, sum(counts.values()), expected_size)
        return None
    return counts
