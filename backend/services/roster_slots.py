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
