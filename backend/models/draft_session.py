from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB

from backend.database import Base


class DraftSession(Base):
    """Durable snapshot of a live draft session — one row per user.

    The in-memory DraftSessionManager holds the warm LiveDraftEngine; this row is
    the DURABLE mirror so a process restart (Railway redeploy) or crash mid-draft
    can rehydrate the exact state instead of losing the draft. `user_id` is the
    session key (one active draft per user). `session_state` is
    DraftStateManager.to_dict() — the full rosters/budgets/picks snapshot.

    Deliberately a fresh table, NOT an extension of the legacy `draft_state` table
    (which lacks user_id and can't hold the full snapshot).
    """
    __tablename__ = "draft_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The session key. Unique → one active draft per user. CASCADE so a deleted
    # user takes their session row with them.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    # DraftStateManager.to_dict() — the full mutable draft state.
    session_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    draft_type: Mapped[Optional[str]] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
