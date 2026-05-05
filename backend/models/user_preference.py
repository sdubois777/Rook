"""
User preferences — watchlist, draft strategy, and app settings.

Stores per-user preferences as typed rows with optional JSONB payload.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    preference_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    entity_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    value: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
