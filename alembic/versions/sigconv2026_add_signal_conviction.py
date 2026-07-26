"""add players.signal_conviction — the price-neutral ranking basis

The board's "top opportunities" list was ranked by the dollar value_gap, which is
price-biased: measured on the as-of 2025 backtest, the top 20% of the board by dollar gap
scored 46.7% — WORSE than a coin flip — while the same slice ranked by the standardised
within-position projection residual scored 66.7%. This column stores that residual so the
ranking has something correct to sort on.

Nullable on purpose: it is undefined for players with no market price and for positions
where the points-vs-price curve cannot be fit (K/DEF, or too few priced players). NULL
means "no market-relative opinion", which is exactly what value_assessment already does.

Revision ID: sigconv2026
Revises: adjpts2026
"""
from alembic import op
import sqlalchemy as sa

revision = "sigconv2026"
down_revision = "adjpts2026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no server_default: nothing to backfill, and the reconcile pass
    # (run_predraft_pipeline phase 6) populates it on the next run. No NOT NULL, so this
    # needs no server_default — see the documented migration pitfall.
    op.add_column(
        "players",
        sa.Column("signal_conviction", sa.Float(), nullable=True),
    )
    # Ranking column — the draftboard and the dashboard both sort on it.
    op.create_index(
        "ix_players_signal_conviction", "players", ["signal_conviction"],
    )


def downgrade() -> None:
    op.drop_index("ix_players_signal_conviction", table_name="players")
    op.drop_column("players", "signal_conviction")
