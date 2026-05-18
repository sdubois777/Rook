"""
AI Assistant router — chat interface with full draft bible context.

Endpoint:
  POST /assistant/chat — streaming chat with context-enriched Sonnet calls
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile, PlayerSchedule
from backend.models.dependency import PlayerDependency, BeatReporterSignal
from backend.models.team_system import TeamSystem
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/assistant", tags=["assistant"])


def clean_response(text: str) -> str:
    """Remove markdown formatting from chatbot responses.

    Users see plain text in the chat panel, not rendered markdown.
    """
    # Remove markdown tables
    text = re.sub(r'\|.*\|.*\n?', '', text)
    # Remove headers
    text = re.sub(r'#{1,6}\s+', '', text)
    # Remove bold/italic
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
    # Remove bullet points
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    # Remove numbered lists
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 500

SYSTEM_PROMPT = """You are the user's fantasy football draft advisor. You have access to their full draft bible — player valuations, system grades, dependency flags, injury risk scores, and schedule analysis.

Use the real data to give specific, grounded advice. Reference actual numbers. Don't speak in generalities when you have data.

The two-value system matters: system value is what the player is worth, market value is what the room will pay. The gap is the edge.

RESPONSE RULES — follow exactly:
- Maximum 2-3 sentences per response
- Never show reasoning steps, tables, or internal analysis
- Never use markdown headers, bullet points, or formatted tables in responses
- Give the conclusion only, not how you reached it
- Speak directly to the user as their fantasy draft advisor
- If asked for a lineup recommendation, give the answer — not the methodology
- Be direct and opinionated — no hedged disclaimers"""


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    context_type: str = "general"  # general|player|trade|draft|lineup
    player_ids: list[str] = []
    include_roster: bool = True
    include_opponents: bool = False
    conversation_history: list[ChatMessage] = []


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

async def build_assistant_context(
    message: str,
    context_type: str,
    player_ids: list[str],
    include_roster: bool,
    include_opponents: bool,
) -> str:
    """
    Build the context block injected into the assistant request.
    Pulls relevant data from the draft bible database.
    """
    sections = []

    # Always include league context
    sections.append("""LEAGUE CONTEXT:
Format: 12-team PPR (1pt per reception)
Budget: $200 auction (skill starters target: $185)
Roster: 1 QB, 2 RB, 2 WR, 1 FLEX (RB/WR/TE), 1 TE, 1 K, 1 DEF, 7 bench
Playoff weeks: 14-17
Positional budget targets: RB=38%, WR=32%, QB=10%, TE=10% of $185
Max realistic bids: RB=$80, WR=$70, QB=$50, TE=$45""")

    async with AsyncSessionLocal() as session:
        # Load all team systems for current season (one query, O(1) lookup by team)
        team_systems_map = await _load_team_systems(session)

        # Include explicitly requested players
        explicit_players = []
        if player_ids:
            parsed_ids = []
            for pid in player_ids:
                try:
                    parsed_ids.append(uuid.UUID(pid))
                except (ValueError, TypeError):
                    continue
            if parsed_ids:
                stmt = (
                    select(Player)
                    .options(
                        selectinload(Player.profile),
                        selectinload(Player.injury_profile),
                        selectinload(Player.schedule),
                        selectinload(Player.dependencies),
                        selectinload(Player.beat_signals),
                    )
                    .where(Player.id.in_(parsed_ids))
                )
                result = await session.execute(stmt)
                explicit_players = list(result.scalars().all())
                for player in explicit_players:
                    sections.append(_format_player_context(player, team_systems_map))

        # Auto-detect players mentioned in the message
        mentioned = await _find_mentioned_players(session, message)
        explicit_ids = {p.id for p in explicit_players}
        for player in mentioned:
            if player.id not in explicit_ids:
                sections.append(_format_player_context(player, team_systems_map))

        # Always include the full draft board (compact format) so the model
        # can answer any question about any player
        draft_board = await _get_full_draft_board(session)
        if draft_board:
            sections.append(draft_board)

        # Include team systems overview for draft/general questions
        if context_type in ("draft", "general"):
            overview = _format_team_systems_overview(team_systems_map)
            if overview:
                sections.append(overview)

        # Include top value gaps for draft strategy questions
        if context_type in ("draft", "general"):
            gaps = await _get_top_value_gaps(session)
            if gaps:
                sections.append(gaps)

        # Include recent relevant news
        all_player_ids = [p.id for p in explicit_players] + [p.id for p in mentioned if p.id not in explicit_ids]
        news = await _get_recent_signals(session, all_player_ids)
        if news:
            sections.append(news)

    context = "\n\n---\n\n".join(sections)
    logger.info(
        "Assistant context built: %d sections, %d chars, %d team_systems, context_type=%s",
        len(sections), len(context), len(team_systems_map), context_type,
    )
    return context


def _format_player_context(player: Player, team_systems_map: dict[str, TeamSystem]) -> str:
    """Format a single player's full draft bible context."""
    lines = [f"PLAYER: {player.name} ({player.position}, {player.team_abbr or 'FA'})"]

    # Valuation
    if player.tier:
        lines.append(f"  Tier: {player.tier}")
    if player.recommended_bid_ceiling:
        lines.append(f"  Bid ceiling: ${player.recommended_bid_ceiling}")
    if player.baseline_value:
        lines.append(f"  System value: ${player.baseline_value}")
    if player.market_value:
        lines.append(f"  Market value: ${player.market_value}")
    if player.value_gap and player.value_gap_signal:
        lines.append(f"  Value gap: ${player.value_gap} ({player.value_gap_signal})")
    if player.ceiling_value:
        lines.append(f"  Ceiling value: ${player.ceiling_value}")
    if player.floor_value:
        lines.append(f"  Floor value: ${player.floor_value}")
    if player.risk_adjusted_value:
        lines.append(f"  Risk-adjusted value: ${player.risk_adjusted_value}")
    if player.situation_score:
        lines.append(f"  Situation: {player.situation_score}")
    if player.let_go_threshold:
        lines.append(f"  Let-go threshold: ${player.let_go_threshold}")
    if player.positional_scarcity_modifier:
        lines.append(f"  Positional scarcity modifier: {float(player.positional_scarcity_modifier):+.2f}")
    if player.breakout_flag:
        lines.append("  BREAKOUT CANDIDATE")

    # Rookie info
    if player.is_rookie:
        lines.append("  Rookie: Yes")
        if player.draft_capital_signal:
            lines.append(f"  Draft capital: {player.draft_capital_signal}")
        if player.historical_comp_names:
            lines.append(f"  Historical comps: {', '.join(player.historical_comp_names)}")
        if player.comp_yr1_avg_ppg:
            lines.append(f"  Comp yr1 avg PPG: {player.comp_yr1_avg_ppg}")
        if player.landing_spot_modifier:
            lines.append(f"  Landing spot modifier: {float(player.landing_spot_modifier):+.3f}")
        if player.projection_confidence:
            lines.append(f"  Projection confidence: {player.projection_confidence}")

    # Profile data
    if player.profile:
        p = player.profile
        if p.role_classification:
            lines.append(f"  Role: {p.role_classification}")
        if p.career_trajectory:
            lines.append(f"  Career trajectory: {p.career_trajectory}")
        if p.clean_season_baseline:
            csb = p.clean_season_baseline
            ppr = csb.get("ppr_points")
            rec = csb.get("receptions")
            yds = csb.get("yards")
            tds = csb.get("tds")
            if ppr:
                lines.append(f"  Clean season baseline: {rec} rec, {yds} yds, {tds} TD, {ppr:.0f} PPR pts")
        if p.target_share_last_season:
            lines.append(f"  Target share (last season): {float(p.target_share_last_season):.1%}")
        if p.snap_percentage:
            lines.append(f"  Snap %: {float(p.snap_percentage):.1%}")
        if p.efficiency_signal:
            lines.append(f"  Efficiency: {p.efficiency_signal}")
        if p.positional_scarcity_tier:
            lines.append(f"  Positional scarcity: {p.positional_scarcity_tier}")
        if p.breakout_flag:
            reason = f" — {p.breakout_reasoning[:80]}" if p.breakout_reasoning else ""
            lines.append(f"  Breakout flag: Yes{reason}")
        # Rookie profile extras
        if p.is_rookie:
            if p.year1_role:
                lines.append(f"  Year 1 role: {p.year1_role}")
            if p.breakout_window:
                lines.append(f"  Breakout window: {p.breakout_window}")
            if p.ceiling_value_ppr:
                lines.append(f"  Ceiling PPR: {p.ceiling_value_ppr}")
            if p.floor_value_ppr:
                lines.append(f"  Floor PPR: {p.floor_value_ppr}")

    # Injury
    if player.injury_profile:
        ip = player.injury_profile
        lines.append(f"  Injury risk: {ip.overall_risk_level or 'unknown'}")
        if ip.post_acl_flag:
            lines.append("  Flag: POST_ACL recovery")
        if ip.workload_cliff_flag:
            lines.append("  Flag: WORKLOAD_CLIFF")
        if ip.high_mileage_flag:
            lines.append(f"  Flag: HIGH_MILEAGE ({ip.career_carry_count or '?'} career carries)")
        if ip.pattern_flags:
            for flag in ip.pattern_flags:
                if flag not in ("POST_ACL", "WORKLOAD_CLIFF", "HIGH_MILEAGE"):
                    lines.append(f"  Flag: {flag}")
        if ip.recovery_assessment:
            lines.append(f"  Recovery: {ip.recovery_assessment}")
        if ip.risk_adjusted_value_modifier:
            lines.append(f"  Risk modifier: {float(ip.risk_adjusted_value_modifier):+.0%}")
        if ip.risk_notes:
            lines.append(f"  Injury notes: {ip.risk_notes[:120]}")

    # Schedule
    if player.schedule:
        s = player.schedule
        parts = []
        if s.early_window_grade:
            parts.append(f"Early: {s.early_window_grade}")
        if s.full_season_grade:
            parts.append(f"Full: {s.full_season_grade}")
        if s.playoff_window_grade:
            parts.append(f"Playoffs: {s.playoff_window_grade}")
        if parts:
            lines.append(f"  Schedule: {', '.join(parts)}")
        if s.bye_week:
            lines.append(f"  Bye week: {s.bye_week}")
        if s.bye_in_playoff_window:
            lines.append("  Warning: Bye in playoff window")
        if s.schedule_score:
            lines.append(f"  Schedule score: {s.schedule_score}")
        if s.playoff_matchups:
            matchup_strs = [str(m) for m in s.playoff_matchups[:4]]
            lines.append(f"  Playoff matchups: {', '.join(matchup_strs)}")

    # Dependency flags
    if player.dependencies:
        flag_lines = []
        for dep in player.dependencies:
            flag_str = f"    {dep.flag_type.upper()}"
            if dep.trigger_player_name:
                flag_str += f" (trigger: {dep.trigger_player_name})"
            if dep.reasoning:
                flag_str += f" — {dep.reasoning[:100]}"
            flag_lines.append(flag_str)
        if flag_lines:
            lines.append("  Active flags:")
            lines.extend(flag_lines)

    # Team system context
    ts = team_systems_map.get(player.team_abbr) if player.team_abbr else None
    if ts:
        lines.append(f"  Team system ({ts.team_abbr}):")
        lines.append(f"    System grade: {ts.system_grade or '?'} (ceiling: {ts.system_ceiling or '?'})")
        lines.append(f"    O-line — pass protection: {ts.pass_protection_grade or '?'}, run blocking: {ts.run_blocking_grade or '?'}")
        if ts.sack_rate is not None:
            lines.append(f"    Sack rate: {float(ts.sack_rate):.1%}")
        if ts.avg_time_to_throw is not None:
            lines.append(f"    Avg time to throw: {ts.avg_time_to_throw}s")
        lines.append(f"    QB: {ts.qb_name or '?'} ({ts.qb_tier or '?'} tier, mobility: {ts.qb_mobility or '?'})")
        if ts.qb_cpoe is not None:
            lines.append(f"    QB CPOE: {ts.qb_cpoe:+.1f}, AY/A: {ts.qb_air_yards_per_attempt or '?'}")
        if ts.qb_pressure_performance:
            lines.append(f"    QB under pressure: {ts.qb_pressure_performance}")
        if ts.rookie_qb_flag:
            lines.append("    WARNING: Rookie QB")
        if ts.compound_risk_flag:
            lines.append("    SEVERE WARNING: Compound risk (rookie QB + weak O-line)")
        lines.append(f"    OC: {ts.oc_name or '?'} — {ts.oc_scheme or '?'} scheme")
        if ts.oc_run_pass_split_tendency is not None:
            lines.append(f"    Pass rate tendency: {float(ts.oc_run_pass_split_tendency):.0%}")
        if ts.personnel_tendency:
            lines.append(f"    Personnel: {ts.personnel_tendency}")
        if ts.red_zone_philosophy:
            lines.append(f"    Red zone: {ts.red_zone_philosophy}")

    # Notes
    if player.notes:
        lines.append(f"  Notes: {player.notes}")

    return "\n".join(lines)


async def _load_team_systems(session) -> dict[str, TeamSystem]:
    """Load all team systems for the current season into a dict keyed by team_abbr."""
    season = get_current_season()
    stmt = select(TeamSystem).where(TeamSystem.season_year == season)
    result = await session.execute(stmt)
    systems = result.scalars().all()
    return {ts.team_abbr: ts for ts in systems}


def _format_team_systems_overview(team_systems_map: dict[str, TeamSystem]) -> str | None:
    """Compact overview of all 32 team systems for broad draft questions."""
    if not team_systems_map:
        return None

    lines = ["TEAM SYSTEMS OVERVIEW (current season):"]
    # Sort by system grade for readability
    grade_order = {"A+": 0, "A": 1, "A-": 2, "B+": 3, "B": 4, "B-": 5,
                   "C+": 6, "C": 7, "C-": 8, "D+": 9, "D": 10, "F": 11}
    sorted_teams = sorted(
        team_systems_map.values(),
        key=lambda ts: grade_order.get(ts.system_grade or "F", 99),
    )
    for ts in sorted_teams:
        flags = []
        if ts.compound_risk_flag:
            flags.append("COMPOUND_RISK")
        if ts.rookie_qb_flag:
            flags.append("ROOKIE_QB")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"  {ts.team_abbr}: grade={ts.system_grade or '?'}, "
            f"QB={ts.qb_name or '?'} ({ts.qb_tier or '?'}), "
            f"OL pass={ts.pass_protection_grade or '?'}/run={ts.run_blocking_grade or '?'}, "
            f"scheme={ts.oc_scheme or '?'}{flag_str}"
        )
    return "\n".join(lines)


async def _find_mentioned_players(session, message: str) -> list[Player]:
    """Find players mentioned by name in the user's message."""
    # Load all players with valuations (only those with tiers are relevant)
    stmt = (
        select(Player)
        .options(
            selectinload(Player.profile),
            selectinload(Player.injury_profile),
            selectinload(Player.schedule),
            selectinload(Player.dependencies),
        )
        .where(Player.tier.isnot(None))
    )
    result = await session.execute(stmt)
    all_players = result.scalars().all()

    msg_lower = message.lower()
    found = []

    for player in all_players:
        if not player.name:
            continue
        parts = player.name.split()
        # Match on last name (3+ chars to avoid false positives)
        last_name = parts[-1].lower() if parts else ""
        if len(last_name) >= 3 and last_name in msg_lower:
            # Verify it's a word boundary match
            pattern = r'\b' + re.escape(last_name) + r'\b'
            if re.search(pattern, msg_lower):
                found.append(player)

    # Limit to 5 to avoid massive context
    return found[:5]


async def _get_full_draft_board(session) -> str | None:
    """Compact one-line summary of every tiered player — the full draft board."""
    stmt = (
        select(Player)
        .where(Player.tier.isnot(None))
        .order_by(Player.tier, Player.baseline_value.desc())
    )
    result = await session.execute(stmt)
    players = result.scalars().all()

    if not players:
        return None

    lines = ["FULL DRAFT BOARD (all tiered players):"]
    current_tier = None
    for p in players:
        if p.tier != current_tier:
            current_tier = p.tier
            lines.append(f"  --- Tier {current_tier} ---")

        parts = [f"{p.name} ({p.position}, {p.team_abbr or 'FA'})"]
        if p.recommended_bid_ceiling is not None:
            parts.append(f"ceil=${p.recommended_bid_ceiling}")
        if p.baseline_value is not None:
            parts.append(f"sys=${p.baseline_value}")
        if p.market_value is not None:
            parts.append(f"mkt=${p.market_value}")
        if p.value_gap and p.value_gap_signal:
            parts.append(f"gap={p.value_gap_signal}")
        if p.situation_score:
            parts.append(f"sit={p.situation_score}")
        if p.breakout_flag:
            parts.append("BREAKOUT")

        lines.append(f"  {' | '.join(parts)}")

    return "\n".join(lines)


async def _get_top_value_gaps(session) -> str | None:
    """Get top 10 players where system value exceeds market value."""
    stmt = (
        select(Player)
        .where(
            Player.value_gap.isnot(None),
            Player.value_gap > 0,
            Player.tier.isnot(None),
        )
        .order_by(Player.value_gap.desc())
        .limit(10)
    )
    result = await session.execute(stmt)
    players = result.scalars().all()

    if not players:
        return None

    lines = ["TOP VALUE GAPS (system value > market value — buy-low targets):"]
    for p in players:
        lines.append(
            f"  {p.name} ({p.position}, {p.team_abbr}) — "
            f"Gap: +${p.value_gap}, Ceil: ${p.recommended_bid_ceiling}, T{p.tier}"
        )
    return "\n".join(lines)


async def _get_recent_signals(session, player_ids: list) -> str | None:
    """Get recent beat reporter signals for relevant players."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    stmt = select(BeatReporterSignal).where(
        BeatReporterSignal.flagged_at >= cutoff
    ).order_by(BeatReporterSignal.flagged_at.desc()).limit(10)

    # If we have specific player IDs, prioritize those
    if player_ids:
        stmt = select(BeatReporterSignal).where(
            BeatReporterSignal.player_id.in_(player_ids),
            BeatReporterSignal.flagged_at >= cutoff,
        ).order_by(BeatReporterSignal.flagged_at.desc()).limit(10)

    result = await session.execute(stmt)
    signals = result.scalars().all()

    if not signals:
        return None

    lines = ["RECENT NEWS (last 14 days):"]
    for s in signals:
        date_str = s.flagged_at.strftime("%b %d") if s.flagged_at else "?"
        lines.append(f"  [{date_str}] {s.signal_type}: {s.raw_text or 'No details'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/debug-context")
async def debug_context(request: ChatRequest):
    """Debug endpoint — returns the raw context that would be sent to the model."""
    context = await build_assistant_context(
        message=request.message,
        context_type=request.context_type,
        player_ids=request.player_ids,
        include_roster=request.include_roster,
        include_opponents=request.include_opponents,
    )
    return {"context": context, "context_length": len(context)}


@router.post("/chat")
async def chat(request: ChatRequest):
    """Stream a response from the AI assistant with full draft bible context."""
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Build context from database
    context = await build_assistant_context(
        message=request.message,
        context_type=request.context_type,
        player_ids=request.player_ids,
        include_roster=request.include_roster,
        include_opponents=request.include_opponents,
    )

    # Build messages list with conversation history
    messages = []
    for msg in request.conversation_history[-10:]:  # Last 10 messages max
        messages.append({"role": msg.role, "content": msg.content})

    # Add current message with context + style reinforcement
    user_content = f"""Answer in 1-3 sentences. No tables, no bullet points, no reasoning steps. Just the direct answer.

Here is the relevant data from my draft bible:

{context}

---

My question: {request.message}"""

    messages.append({"role": "user", "content": user_content})

    async def generate():
        try:
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            async with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    cleaned = clean_response(text)
                    if cleaned:
                        yield f"data: {json.dumps({'text': cleaned})}\n\n"
            yield "data: [DONE]\n\n"
        except anthropic.APIError as e:
            logger.error("Anthropic API error in assistant: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error("Unexpected error in assistant: %s", e)
            yield f"data: {json.dumps({'error': 'An unexpected error occurred'})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
