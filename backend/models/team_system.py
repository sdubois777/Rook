from __future__ import annotations
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import String, Integer, Boolean, DateTime, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class TeamSystem(Base):
    """Agent 1 output — offensive system grade per NFL team per season."""
    __tablename__ = "team_systems"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_abbr: Mapped[str] = mapped_column(String(5), nullable=False)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)

    # O-line grades (split — they do not always correlate)
    pass_protection_grade: Mapped[Optional[str]] = mapped_column(String(5))   # A+, A, A-, B+, ...
    run_blocking_grade: Mapped[Optional[str]] = mapped_column(String(5))

    # QB profile
    qb_name: Mapped[Optional[str]] = mapped_column(String(100))
    qb_tier: Mapped[Optional[str]] = mapped_column(String(20))   # elite/solid/average/weak
    qb_experience_years: Mapped[Optional[int]] = mapped_column(Integer)
    qb_pressure_performance: Mapped[Optional[str]] = mapped_column(String(20))  # elite/above_avg/avg/below_avg
    qb_cpoe: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))           # completion % over expectation
    qb_air_yards_per_attempt: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    qb_downfield_aggressiveness: Mapped[Optional[str]] = mapped_column(String(20))  # aggressive/moderate/conservative

    # Risk flags
    rookie_qb_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    # compound_risk_flag: true when rookie QB AND pass protection C or below
    # Cascades as severe penalty to all skill position players on roster
    compound_risk_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # Offensive coordinator
    oc_name: Mapped[Optional[str]] = mapped_column(String(100))
    oc_scheme: Mapped[Optional[str]] = mapped_column(String(30))   # balanced/pass_heavy/run_heavy/west_coast/air_raid
    oc_run_pass_split_tendency: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))  # pass rate (0-1)
    personnel_tendency: Mapped[Optional[str]] = mapped_column(String(10))  # 11/12/21/etc
    red_zone_philosophy: Mapped[Optional[str]] = mapped_column(String(20)) # wr1/te/rb/spread

    # Numeric O-line metrics (Python-computed, stored for querying)
    sack_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))      # sacks / dropbacks
    avg_time_to_throw: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))  # seconds
    qb_mobility: Mapped[Optional[str]] = mapped_column(String(20))           # elite/average/pocket_only

    # System summary
    system_ceiling: Mapped[Optional[str]] = mapped_column(String(20))  # high/moderate/low
    system_grade: Mapped[Optional[str]] = mapped_column(String(5))     # A+, A, A-, B+, ...
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
