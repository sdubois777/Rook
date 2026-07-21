"""create value_snapshots (immutable pre-season prediction record)

Revision ID: vs2026snap01
Revises: e2f3a4b5c6d7
Create Date: 2026-07 (pre-2026-draft)

Additive: creates one new table. Touches no existing table or data.

NOTE ON ALEMBIC STATE (flagged during build): the DB is stamped at e2f3a4b5c6d7 while the
migration tree has several unmerged branch-leaves and the live schema is ahead of the stamp
(e.g. sportradar_id exists but isn't in this stamp's ancestry). This revision chains off the
APPLIED revision so `alembic upgrade vs2026snap01` applies cleanly on the current DB; it does
not attempt to fix the pre-existing multi-head history.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "vs2026snap01"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "value_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # identity (merge-safe)
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("scoring_format", sa.String(length=10), nullable=False),
        sa.Column("snapshot_label", sa.String(length=40), nullable=False),
        sa.Column("player_id", UUID(as_uuid=True),
                  sa.ForeignKey("players.id", ondelete="SET NULL"), nullable=True),
        sa.Column("gsis_id", sa.String(length=20), nullable=True),
        sa.Column("sportradar_id", sa.String(length=50), nullable=True),
        sa.Column("sleeper_id", sa.String(length=50), nullable=True),
        sa.Column("player_name", sa.String(length=100), nullable=False),
        sa.Column("position", sa.String(length=5), nullable=True),
        # our value (effective per-format)
        sa.Column("projected_ppr", sa.Numeric(6, 1), nullable=True),
        sa.Column("replacement_ppr", sa.Numeric(6, 1), nullable=True),
        sa.Column("par_ratio", sa.Numeric(5, 3), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=True),
        sa.Column("baseline_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("recommended_bid_ceiling", sa.Numeric(5, 2), nullable=True),
        sa.Column("ceiling_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("floor_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("risk_adjusted_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("ai_bid_ceiling", sa.Integer(), nullable=True),
        sa.Column("value_gap", sa.Numeric(5, 1), nullable=True),
        sa.Column("value_gap_signal", sa.String(length=20), nullable=True),
        sa.Column("value_assessment", sa.String(length=20), nullable=True),
        sa.Column("pay_up_flag", sa.Boolean(), nullable=True),
        sa.Column("nomination_target_flag", sa.Boolean(), nullable=True),
        # market side + provenance
        sa.Column("market_value_fantasypros", sa.Numeric(5, 2), nullable=True),
        sa.Column("adp_fantasypros", sa.Numeric(5, 1), nullable=True),
        sa.Column("market_value_league", sa.Numeric(5, 2), nullable=True),
        sa.Column("market_source", sa.String(length=60), nullable=True),
        sa.Column("market_fetched_at", sa.DateTime(timezone=True), nullable=True),
        # engine identity
        sa.Column("valuation_agent_version", sa.String(length=10), nullable=True),
        sa.Column("profiles_prompt_version", sa.String(length=10), nullable=True),
        sa.Column("git_sha", sa.String(length=12), nullable=True),
        sa.Column("pipeline_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # outcome side (write-back)
        sa.Column("actual_points", sa.Numeric(6, 1), nullable=True),
        sa.Column("actual_games", sa.Integer(), nullable=True),
        sa.Column("outcome_written_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("gsis_id", "season_year", "scoring_format", "snapshot_label",
                            name="uq_value_snapshot_gsis"),
    )
    # partial unique index for the ~8 gsis-less players (name+position identity)
    op.create_index(
        "uq_value_snapshot_nogsis", "value_snapshots",
        ["player_name", "position", "season_year", "scoring_format", "snapshot_label"],
        unique=True, postgresql_where=sa.text("gsis_id IS NULL"),
    )
    op.create_index("ix_value_snapshot_join", "value_snapshots",
                    ["gsis_id", "season_year", "scoring_format"])
    op.create_index("ix_value_snapshot_season_label", "value_snapshots",
                    ["season_year", "snapshot_label"])
    op.create_index("ix_value_snapshots_gsis_id", "value_snapshots", ["gsis_id"])


def downgrade() -> None:
    op.drop_index("ix_value_snapshots_gsis_id", table_name="value_snapshots")
    op.drop_index("ix_value_snapshot_season_label", table_name="value_snapshots")
    op.drop_index("ix_value_snapshot_join", table_name="value_snapshots")
    op.drop_index("uq_value_snapshot_nogsis", table_name="value_snapshots")
    op.drop_table("value_snapshots")
