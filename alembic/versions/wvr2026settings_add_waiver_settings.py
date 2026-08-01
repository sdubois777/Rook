"""store each league's real waiver settings

Revision ID: wvr2026settings
Revises: src2026tenant
Create Date: 2026-07-31

WHY. The waiver recommendation charged 2 credits and then computed a dollar bid
against a hardcoded $100 budget that was never read from the league, and the
waiver page displayed "$100 of $100 budget left" for every customer regardless of
their actual league. Leagues that use rolling waiver priority — which do not bid
at all — were labelled as budget leagues and given a dollar figure anyway.

These columns hold what the platform actually reports.

uses_bidding_budget is deliberately TRI-STATE and nullable:
  NULL  = we could not determine the waiver system; claim nothing
  True  = the league bids with a budget
  False = priority or reverse standings; a dollar bid is meaningless

It must NOT be inferred from waiver_budget. Measured across 285 live Sleeper
leagues, a waiver_budget value was present on 285 of 285 — including every
rolling-waiver league, almost always as a vestigial 100. Reading the budget
without gating on the system first reproduces the exact fabricated figure this
change exists to remove. ESPN behaves the same way: 34 of 35 live non-bidding
leagues still carried an acquisitionBudget of 100.

waiver_type is widened from 20 to 30 characters because it now holds a readable
label such as "continuous waiver priority" rather than a short platform code.
A VARCHAR length increase is a catalog change in Postgres — no table rewrite, no
long lock — which matters because railway.toml starts the service with
`alembic upgrade head && uvicorn`, so a slow or failing migration is downtime.

No backfill. Every existing league is left with NULL, which reads as "unknown"
and causes the product to withhold a budget claim rather than invent one. The
values populate on the next league sync.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'wvr2026settings'
down_revision: Union[str, None] = 'src2026tenant'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_leagues',
        sa.Column('uses_bidding_budget', sa.Boolean(), nullable=True),
    )
    op.add_column(
        'user_leagues',
        sa.Column('waiver_budget', sa.Integer(), nullable=True),
    )
    # Widen for the readable label. Length INCREASE only — catalog-only in Postgres.
    op.alter_column(
        'user_leagues', 'waiver_type',
        existing_type=sa.String(20), type_=sa.String(30),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Drops both columns, losing the stored waiver settings.

    That is acceptable and recoverable: the values are re-read from the platform
    on the next league sync. Narrowing waiver_type back to 20 characters could
    fail on a stored label longer than that, so any such value is truncated
    first rather than letting the ALTER abort.
    """
    op.execute(sa.text(
        "UPDATE user_leagues SET waiver_type = left(waiver_type, 20) "
        "WHERE waiver_type IS NOT NULL AND length(waiver_type) > 20"
    ))
    op.alter_column(
        'user_leagues', 'waiver_type',
        existing_type=sa.String(30), type_=sa.String(20),
        existing_nullable=True,
    )
    op.drop_column('user_leagues', 'waiver_budget')
    op.drop_column('user_leagues', 'uses_bidding_budget')
