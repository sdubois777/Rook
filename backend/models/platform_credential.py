"""
Per-user platform credentials.
Tokens encrypted at rest. Never stored plaintext.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class PlatformCredential(Base):
    __tablename__ = "platform_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_platform_credentials_user_platform"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    # "yahoo" | "espn" | "sleeper"

    # Yahoo OAuth tokens (encrypted)
    access_token: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    refresh_token: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ESPN cookies (encrypted)
    espn_s2: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    swid: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )

    # Sleeper (no auth — just user ID)
    sleeper_user_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
