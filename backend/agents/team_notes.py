"""
Teams-page system-notes regeneration (Teams rework slice 3, Part 2).

system_notes is the ONE legitimately GENERATIVE Teams-page field — narrative
synthesis is the LLM's honest job. But the recon found it INVENTS numbers (cpoe
62.5, a fabricated 0.38 pass rate) because the old agent generated the notes from
its own hallucinated stats. Now that every factual field is a real computed numeric
(slices 1+2) with widened-bell grades (slice 3 Part 1), this pass REGENERATES the
notes AFTER team_metrics — feeding Haiku ONLY the real stored numerics + grades and
instructing it to narrate FROM those numbers, never invent. It runs after the bell
grades so it also has the final letter grades.

Cheap (Haiku, ~150 tokens). Client-injectable so unit tests don't hit the API.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from backend.agents.base_agent import HAIKU, get_client
from backend.models.team_system import TeamSystem

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an NFL fantasy analyst writing a 2-sentence system note for one team. "
    "CRITICAL: use ONLY the real stats provided in the input — every number you write "
    "MUST appear in that input. Do NOT invent, estimate, or add any statistic (no made-up "
    "sack rates, cpoe, target shares, or percentages). You MAY reference the provided "
    "grades and labels (scheme, pass-protection, run-blocking, QB tier, red-zone). Focus "
    "on FANTASY implications. Output ONLY the 2 sentences — no preamble, no markdown."
)


def _fmt(v, pct: bool = False, dec: int = 2) -> str:
    if v is None:
        return "n/a"
    f = float(v)
    return f"{f * 100:.1f}%" if pct else f"{f:.{dec}f}"


def build_notes_input(ts: TeamSystem) -> str:
    """The REAL-stats block fed to the model — every number here is a computed value
    stored on the TeamSystem row (no fabrication possible downstream)."""
    return (
        f"team: {ts.team_abbr}\n"
        f"QB: {ts.qb_name or 'n/a'} (tier: {ts.qb_tier or 'n/a'}, "
        f"rookie: {bool(ts.rookie_qb_flag)}, cpoe: {_fmt(ts.qb_cpoe)})\n"
        f"scheme: {ts.oc_scheme or 'n/a'} (pass rate {_fmt(ts.oc_run_pass_split_tendency, pct=True)})\n"
        f"pass protection: grade {ts.pass_protection_grade or 'n/a'} "
        f"(sack rate {_fmt(ts.sack_rate, pct=True)})\n"
        f"run blocking: grade {ts.run_blocking_grade or 'n/a'} "
        f"(stuff rate {_fmt(ts.run_block_stuff_rate, pct=True)})\n"
        f"system grade: {ts.system_grade or 'n/a'}\n"
        f"base personnel: {ts.personnel_tendency or 'n/a'}\n"
        f"red-zone weapon: {ts.red_zone_philosophy or 'n/a'}"
    )


async def regenerate_team_notes(
    db,
    *,
    limit: Optional[int] = None,
    client=None,
) -> dict:
    """Regenerate system_notes for the latest TeamSystem season from the REAL stored
    stats. ``limit`` caps how many teams (verification / cost control). ``client`` is
    injectable (tests). Returns a summary; loud-warns any team whose note fails."""
    client = client or get_client()
    rows = (await db.execute(select(TeamSystem))).scalars().all()
    latest = max((r.season_year for r in rows), default=None)
    rows = [r for r in rows if r.season_year == latest]
    if limit is not None:
        rows = rows[:limit]

    written = failed = 0
    for r in rows:
        try:
            resp = await client.messages.create(
                model=HAIKU,
                max_tokens=180,
                system=_SYSTEM,
                messages=[{"role": "user", "content": build_notes_input(r)}],
            )
            text = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            if text:
                r.notes = text
                written += 1
            else:
                failed += 1
                logger.warning("team_notes: empty response for %s — notes left as-is", r.team_abbr)
        except Exception as exc:
            failed += 1
            logger.warning("team_notes: regeneration failed for %s (%s) — notes left as-is", r.team_abbr, exc)

    await db.commit()
    logger.info("team_notes: regenerated %d note(s) from real stats, %d failed", written, failed)
    return {"written": written, "failed": failed, "teams": len(rows)}
