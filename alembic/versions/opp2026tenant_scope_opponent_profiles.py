"""scope opponent_profiles to a tenant

Revision ID: opp2026tenant
Revises: sigconv2026
Create Date: 2026-07-30

``opponent_profiles`` had no user column of any kind, while being READ on every
user's live-draft path (``load_manager_tendencies`` -> ``OpponentThreatAnalyzer``
via backend/routers/draft.py). One league's manager tendencies therefore biased
threat scoring for every user, and ``build_manager_profiles`` deleted every row for
the analysis year regardless of owner.

BACKFILL, NOT DELETE. The existing rows are the operator's own league. Deleting
them is simpler but empties ``manager_styles`` in the live-draft explain context
and drops the positional-bias multiplier from threat scoring — a regression in the
one draft that matters, and nothing repopulates the table because
``build_manager_profiles`` has no callers.

Attribution rule: an ``opponent_profiles`` row carries ``yahoo_team_id`` shaped
``<game_key>.l.<league_id>.t.<team>``, and ``user_leagues.league_id`` IS that
platform league id. So the owner is the user_league with the matching league_id.

DO NOT attribute via ``league_auction_history.league_key``. That was the first
version of this migration and it is silently wrong: the multi-tenant writer
``LeagueSyncService._store_picks`` sets user_id/user_league_id but NEVER league_key,
while the legacy single-tenant ``_sync_season`` sets league_key but never the tenant
columns. No row carries both — verified against backups/prod_pre2026run.sql: 340
tenanted rows, 0 with league_key, 0 joinable. That join matches nothing in
production, and because the old guard reused the same ``league_key IS NOT NULL``
filter to decide whether anything was attributable, it would have concluded
"provably orphaned" and DELETED all 12 production rows — the exact outcome this
docstring exists to prevent.

The guard is therefore deliberately INDEPENDENT of the join: it asks whether any
tenant exists at all, not whether the join found one.

This migration ABORTS rather than guessing. It refuses to run if a row's owner is
ambiguous or unresolvable, because the alternative is a half-populated table that
then fails the SET NOT NULL mid-migration — the worst failure ordering. Rows are
deleted only when provably orphaned (no user_leagues exist at all), which is dev.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = 'opp2026tenant'
down_revision: Union[str, None] = 'sigconv2026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column('opponent_profiles',
        sa.Column('user_id', UUID(as_uuid=True), nullable=True))
    op.add_column('opponent_profiles',
        sa.Column('user_league_id', UUID(as_uuid=True), nullable=True))

    # --- backfill -----------------------------------------------------------
    # AMBIGUITY GUARD FIRST. league_id is NOT unique across user_leagues (prod has
    # three rows sharing 141688), so check before writing rather than after — the
    # UPDATE would otherwise pick an arbitrary one and there is no way to tell
    # afterwards that it guessed.
    ambiguous = conn.execute(sa.text("""
        SELECT count(*) FROM (
            SELECT ul.league_id
            FROM user_leagues ul
            JOIN opponent_profiles op
              ON op.yahoo_team_id IS NOT NULL
             AND split_part(op.yahoo_team_id, '.', 3) = ul.league_id
            GROUP BY ul.league_id
            HAVING count(DISTINCT ul.id) > 1
        ) x
    """)).scalar()
    if ambiguous:
        raise RuntimeError(
            f"{ambiguous} league_id(s) referenced by opponent_profiles map to more "
            "than one user_league — ownership is ambiguous. Resolve before "
            "migrating; do NOT let this fall through to SET NOT NULL."
        )

    # split_part on the league component of '<game>.l.<league_id>.t.<team>', so a
    # team id cannot partial-match a league whose id merely shares a prefix
    # (78512 vs 785120), which a LIKE would.
    conn.execute(sa.text("""
        UPDATE opponent_profiles op
        SET user_id = ul.user_id,
            user_league_id = ul.id
        FROM user_leagues ul
        WHERE op.yahoo_team_id IS NOT NULL
          AND split_part(op.yahoo_team_id, '.', 3) = ul.league_id
    """))

    # Rows we could not attribute. Deleting is safe ONLY when there was nothing to
    # attribute against at all.
    #
    # This guard is deliberately INDEPENDENT of the join above — it counts every
    # user_league, not the ones the join matched. The previous version reused the
    # join's own filter, which meant it could never detect "a tenant exists but the
    # join failed to find it" and would silently delete instead of aborting. That is
    # the single most dangerous shape a backfill guard can have.
    unmatched = conn.execute(sa.text(
        "SELECT count(*) FROM opponent_profiles WHERE user_id IS NULL"
    )).scalar()
    if unmatched:
        tenants = conn.execute(sa.text(
            "SELECT count(*) FROM user_leagues"
        )).scalar()
        if tenants:
            raise RuntimeError(
                f"{unmatched} opponent_profiles row(s) could not be attributed, but "
                f"{tenants} user_league(s) exist to attribute against. This is a real "
                "gap — inspect yahoo_team_id on those rows against "
                "user_leagues.league_id. Refusing to delete data or to proceed to "
                "SET NOT NULL."
            )
        # Provably orphaned: no tenant exists at all (the dev shape).
        conn.execute(sa.text(
            "DELETE FROM opponent_profiles WHERE user_id IS NULL"))

    op.alter_column('opponent_profiles', 'user_id', nullable=False)
    op.alter_column('opponent_profiles', 'user_league_id', nullable=False)

    op.create_foreign_key(
        'fk_opponent_profiles_user_id', 'opponent_profiles', 'users',
        ['user_id'], ['id'], ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_opponent_profiles_user_league_id', 'opponent_profiles', 'user_leagues',
        ['user_league_id'], ['id'], ondelete='CASCADE',
    )
    op.create_index(
        'ix_opponent_profiles_user_id', 'opponent_profiles', ['user_id'])
    op.create_index(
        'ix_opponent_profiles_user_league_id', 'opponent_profiles', ['user_league_id'])
    op.create_unique_constraint(
        'uq_opponent_profile_league_season_team', 'opponent_profiles',
        ['user_league_id', 'season_year', 'team_name'],
    )


def downgrade() -> None:
    """Lossy by nature: dropping the columns discards the attribution. The rows
    themselves survive, so the operator's profiles are not lost — but any row
    deleted as provably-orphaned during upgrade is gone, and re-running upgrade
    will not bring it back."""
    op.drop_constraint(
        'uq_opponent_profile_league_season_team', 'opponent_profiles', type_='unique')
    op.drop_index('ix_opponent_profiles_user_league_id', table_name='opponent_profiles')
    op.drop_index('ix_opponent_profiles_user_id', table_name='opponent_profiles')
    op.drop_constraint(
        'fk_opponent_profiles_user_league_id', 'opponent_profiles', type_='foreignkey')
    op.drop_constraint(
        'fk_opponent_profiles_user_id', 'opponent_profiles', type_='foreignkey')
    op.drop_column('opponent_profiles', 'user_league_id')
    op.drop_column('opponent_profiles', 'user_id')
