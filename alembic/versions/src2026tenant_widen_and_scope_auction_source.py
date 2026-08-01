"""widen league_auction_history.source and scope it to one league

Revision ID: src2026tenant
Revises: opp2026tenant
Create Date: 2026-07-31

WHY. The two unique constraints on league_auction_history are
(player_id, season_year, source) and (season_year, source, yahoo_player_key).
Neither includes any customer or league column, so `source` is the only thing
separating one customer's draft from another's. The sync writes
"sync_{picked_by_team_id}", and on ESPN that team id is just "1".."12" and on
Sleeper it is a roster id in the same range. Two customers therefore produce
identical values, and the insert carries ON CONFLICT DO NOTHING, so the second
customer's picks are silently discarded.

Yahoo looked immune because its team id is a full team key containing the league
id. It is not: two customers in the SAME Yahoo league produce identical values
too. Production already has this — two different users both hold Yahoo league
141688.

The fix puts the league's own row id into `source`, which makes the value unique
per customer without touching either constraint. Adding the league id to a unique
constraint instead was rejected: Postgres treats NULLs as distinct, and the
backtest fixture rows deliberately carry no league id, so they would stop
de-duplicating and every re-import would duplicate them.

TWO STEPS, ONE MIGRATION, ONE RELEASE.

1. Widen the column. The longest rewritten value measured against production is
   58 characters and the column holds 50. A VARCHAR length INCREASE is a catalog
   change in Postgres (no table rewrite, no long lock), which matters because
   railway.toml starts the service with `alembic upgrade head && uvicorn`, so a
   slow or failing migration is downtime rather than a failed deploy.

2. Rewrite the existing rows. If the code started writing the new format while
   old rows kept the old one, the next re-sync would insert every pick AGAIN,
   because the new value does not conflict with the old. The rewrite and the code
   change must therefore ship together.

The rewrite predicate is deliberately narrow: only rows whose source already
starts with "sync_" AND that carry a league id. It cannot touch the "manual_csv"
backtest fixture rows or any operator-imported baseline, because those have no
league id. It is idempotent — a row already in the new format is excluded by the
final NOT LIKE test.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'src2026tenant'
down_revision: Union[str, None] = 'opp2026tenant'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1 — widen. Catalog-only; safe to run at boot.
    op.alter_column(
        'league_auction_history', 'source',
        existing_type=sa.String(50), type_=sa.String(100),
        existing_nullable=False,
    )

    conn = op.get_bind()

    # Step 2 — rewrite platform-sync rows to carry the league id.
    #
    # substring(source from 5) keeps the leading underscore of the old value, so
    # "sync_461.l.1.t.3" becomes "sync_<uuid>_461.l.1.t.3".
    result = conn.execute(sa.text(r"""
        UPDATE league_auction_history
           SET source = 'sync_' || user_league_id::text || substring(source from 5)
         WHERE source LIKE 'sync\_%'
           AND user_league_id IS NOT NULL
           AND source NOT LIKE 'sync\_' || user_league_id::text || '%'
    """))
    print(f"[src2026tenant] rewrote {result.rowcount} platform-sync source values")

    # Nothing may be left in the old format while carrying a league id, or the
    # next sync would insert duplicates rather than recognising existing rows.
    stragglers = conn.execute(sa.text(r"""
        SELECT count(*) FROM league_auction_history
         WHERE source LIKE 'sync\_%'
           AND user_league_id IS NOT NULL
           AND source NOT LIKE 'sync\_' || user_league_id::text || '%'
    """)).scalar()
    if stragglers:
        raise RuntimeError(
            f"{stragglers} platform-sync rows still hold the old source format. "
            "Leaving them would make the next sync insert every pick again."
        )


def downgrade() -> None:
    """Restores the old source format, then narrows the column.

    DO NOT USE THIS AS AN EMERGENCY ROLLBACK once the upgrade has been live long
    enough for two customers to have written picks for the same league. Verified
    by rehearsal: collapsing two customers' rows back to the identical old-format
    value re-triggers uq_auction_player_season_source, which is the very
    constraint this migration exists to work around. It fails safely — the whole
    downgrade runs in one transaction and rolls back with no corruption — but it
    WILL fail, and it cannot be made to succeed without deciding which customer's
    picks to discard. Roll forward instead.

    It also reverts only the schema, not the writer. backend/services/league_sync.py
    writes the tenant-scoped value unconditionally, so running this without
    reverting that code makes the next sync insert a 58-character value into a
    50-character column and raise StringDataRightTruncation.

    Order matters: narrowing first would fail on any row still holding a value
    longer than 50 characters.
    """
    conn = op.get_bind()
    conn.execute(sa.text(r"""
        UPDATE league_auction_history
           SET source = 'sync' || substring(source from 6 + length(user_league_id::text))
         WHERE source LIKE 'sync\_%'
           AND user_league_id IS NOT NULL
           AND source LIKE 'sync\_' || user_league_id::text || '%'
    """))
    op.alter_column(
        'league_auction_history', 'source',
        existing_type=sa.String(100), type_=sa.String(50),
        existing_nullable=False,
    )
