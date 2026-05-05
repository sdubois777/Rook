"""
News router — beat reporter signals feed.

Endpoints:
  GET /news  — chronological feed with filters (team, player, type, days)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.database import AsyncSessionLocal
from backend.models.dependency import BeatReporterSignal
from backend.models.player import Player

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/news", tags=["news"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class SignalFeedItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    signal_type: str
    source: Optional[str] = None
    raw_text: Optional[str] = None
    confidence: Optional[str] = None
    flagged_at: Optional[str] = None
    player_id: Optional[str] = None
    player_name: Optional[str] = None
    player_team: Optional[str] = None
    player_position: Optional[str] = None


class NewsFeedResponse(BaseModel):
    signals: list[SignalFeedItem]
    total: int
    page: int
    per_page: int
    pages: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=NewsFeedResponse)
async def get_news(
    team: Optional[str] = None,
    player_id: Optional[uuid.UUID] = None,
    days: int = Query(30, ge=1, le=365),
    signal_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
):
    """Beat reporter signals feed with filters."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with AsyncSessionLocal() as session:
        # Base query with player join
        query = (
            select(BeatReporterSignal, Player.name, Player.team_abbr, Player.position)
            .outerjoin(Player, BeatReporterSignal.player_id == Player.id)
            .where(BeatReporterSignal.flagged_at >= cutoff)
        )

        # Filters
        if team:
            query = query.where(Player.team_abbr == team.upper())
        if player_id:
            query = query.where(BeatReporterSignal.player_id == player_id)
        if signal_type:
            query = query.where(BeatReporterSignal.signal_type == signal_type)

        # Count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await session.execute(count_query)
        total = total_result.scalar() or 0

        # Order and paginate
        query = query.order_by(BeatReporterSignal.flagged_at.desc())
        offset = (page - 1) * per_page
        query = query.offset(offset).limit(per_page)

        result = await session.execute(query)
        rows = result.all()

    signals = []
    for row in rows:
        sig = row[0]
        player_name = row[1]
        player_team = row[2]
        player_position = row[3]

        signals.append(SignalFeedItem(
            id=str(sig.id),
            signal_type=sig.signal_type,
            source=sig.source,
            raw_text=sig.raw_text,
            confidence=sig.confidence,
            flagged_at=sig.flagged_at.isoformat() if sig.flagged_at else None,
            player_id=str(sig.player_id) if sig.player_id else None,
            player_name=player_name,
            player_team=player_team,
            player_position=player_position,
        ))

    pages = (total + per_page - 1) // per_page if total > 0 else 1

    return NewsFeedResponse(
        signals=signals,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )
