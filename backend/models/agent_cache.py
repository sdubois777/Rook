"""
AgentCache — stores hashed input/output pairs per agent + entity.

Checked before every API call. A cache hit means zero API cost for that run.
Unique on (agent_name, entity_id, input_hash) — re-running with identical
input data always hits the cache, forcing a fresh call requires clearing the row.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class AgentCache(Base):
    __tablename__ = "agent_cache"
    __table_args__ = (
        UniqueConstraint(
            "agent_name", "entity_id", "input_hash",
            name="uq_agent_cache_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
