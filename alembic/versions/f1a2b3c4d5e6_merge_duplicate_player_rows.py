"""merge duplicate (name, position) player rows (two-source dedup cleanup)

One-time PROD data migration. The Sleeper sync (placeholder gsis + sleeper_id) and the
nflverse seed (real gsis, no sleeper_id) created TWO rows per human because seed_nfl_data
deduped only on yahoo_player_id. Prevention (resolve_or_create) is now live; this collapses
the EXISTING duplicates.

AUTO-MERGE (conservative — cannot fuse two humans): a (name, position) group with EXACTLY
two rows where exactly one has a sleeper_id (the Sleeper origin) and the other has NO
sleeper_id + a real gsis ('00-…') (the nflverse origin), with no conflicting distinct
sportradar_id, and the name is not a junk placeholder. Everything else is HELD:
  * >2 rows (e.g. "Duplicate Player", "Player Invalid"),
  * both rows have a sleeper_id (two distinct Sleeper entities → different humans:
    Chris Harper / Cody Chrest / Francis Owusu / Myles White / Nick Williams /
    Rodney Smith / Ronnie Brown / Zach Miller, and the two Zach Miller TEs),
  * neither has a sleeper_id (Matthew Hibner) or identical-id true dups behind review
    (Joe Horn) — held for manual confirmation.

SURVIVOR = the sleeper_id row (keeps the current IDs + team); the nflverse row's fields are
UNIONED in per-field (fill survivor NULLs; booleans OR'd; gsis prefers the real one; the
valuation is un-split), its child FKs are repointed (conflict-safe), then it is deleted.

BACKOUT: a merge is lossy (rows deleted, FKs repointed) — downgrade() is NOT a real reversal
and raises. Authoritative backout = the pre-migration snapshot. Forensic trail =
player_merge_audit (survivor, loser, the loser's full row as JSON). Runs in ONE transaction
(atomic) and is IDEMPOTENT (a clean table has no eligible groups → no-op).

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
"""
import json
import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Boolean

from backend.models.player import Player

revision = "f1a2b3c4d5e6"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.merge_dupes")

# Groups eligible for the safe two-source auto-merge. Returns (survivor, loser) per group.
_ELIGIBLE = sa.text("""
    WITH dg AS (
        SELECT name, position FROM players
        GROUP BY name, position
        HAVING count(*) = 2
           AND count(*) FILTER (WHERE sleeper_id IS NOT NULL) = 1
           AND count(*) FILTER (WHERE sleeper_id IS NULL AND gsis_id LIKE '00-%') = 1
           AND count(DISTINCT sportradar_id) FILTER (WHERE sportradar_id IS NOT NULL) <= 1
           AND name NOT IN ('Player Invalid', 'Duplicate Player')
    )
    SELECT
      (SELECT id FROM players p WHERE p.name = dg.name AND p.position = dg.position
         AND p.sleeper_id IS NOT NULL) AS survivor,
      (SELECT id FROM players p WHERE p.name = dg.name AND p.position = dg.position
         AND p.sleeper_id IS NULL) AS loser
    FROM dg
""")

# One-to-one child tables (unique player_id): repoint if survivor has none, else drop loser's.
_ONE_TO_ONE = ("player_profiles", "player_injury_profiles", "player_schedules")
# Child tables with a (player_id, ...) unique key: repoint non-conflicting, drop the rest.
_UNIQUE_KEYED = {
    "league_auction_history": ["season_year", "source"],
    "market_value_historic": ["season_year"],
    "player_format_values": ["scoring_format"],   # also ON DELETE CASCADE, but repoint to preserve
}


def _merge_pair(conn, survivor, loser) -> None:
    # 1. Audit snapshot of the loser row (forensic trail + partial recovery).
    loser_row = conn.execute(
        sa.text("SELECT * FROM players WHERE id = :l"), {"l": loser}
    ).mappings().first()
    conn.execute(
        sa.text("""INSERT INTO player_merge_audit
                     (survivor_id, loser_id, loser_name, loser_position, loser_row)
                   VALUES (:s, :l, :n, :p, CAST(:row AS jsonb))"""),
        {"s": survivor, "l": loser, "n": loser_row["name"], "p": loser_row["position"],
         "row": json.dumps({k: str(v) for k, v in dict(loser_row).items()})},
    )

    # 2. Per-field UNION loser → survivor: fill survivor NULLs; OR booleans; prefer a real gsis.
    cols = [c.name for c in Player.__table__.columns if c.name not in ("id", "yahoo_player_id")]
    bool_cols = {c.name for c in Player.__table__.columns if isinstance(c.type, Boolean)}
    sets = []
    for c in cols:
        if c == "gsis_id":
            sets.append("gsis_id = CASE WHEN players.gsis_id LIKE '00-%' THEN players.gsis_id ELSE l.gsis_id END")
        elif c in bool_cols:
            sets.append(f"{c} = players.{c} OR l.{c}")
        else:
            sets.append(f"{c} = COALESCE(players.{c}, l.{c})")
    conn.execute(
        sa.text(f"UPDATE players SET {', '.join(sets)} FROM players l "
                f"WHERE players.id = :s AND l.id = :l"),
        {"s": survivor, "l": loser},
    )

    # 3. Repoint child FKs (conflict-safe).
    for t in _ONE_TO_ONE:
        conn.execute(sa.text(
            f"UPDATE {t} SET player_id = :s WHERE player_id = :l "
            f"AND NOT EXISTS (SELECT 1 FROM {t} c WHERE c.player_id = :s)"),
            {"s": survivor, "l": loser})
        conn.execute(sa.text(f"DELETE FROM {t} WHERE player_id = :l"), {"l": loser})

    # player_dependencies — both player_id and trigger_player_id (no unique key on either).
    conn.execute(sa.text("UPDATE player_dependencies SET player_id = :s WHERE player_id = :l"),
                 {"s": survivor, "l": loser})
    conn.execute(sa.text("UPDATE player_dependencies SET trigger_player_id = :s WHERE trigger_player_id = :l"),
                 {"s": survivor, "l": loser})
    # beat_reporter_signals, season_roster — plain repoint (no conflicting unique key).
    for t in ("beat_reporter_signals", "season_roster"):
        conn.execute(sa.text(f"UPDATE {t} SET player_id = :s WHERE player_id = :l"),
                     {"s": survivor, "l": loser})

    for t, keys in _UNIQUE_KEYED.items():
        cond = " AND ".join(f"c.{k} = {t}.{k}" for k in keys)
        conn.execute(sa.text(
            f"UPDATE {t} SET player_id = :s WHERE player_id = :l "
            f"AND NOT EXISTS (SELECT 1 FROM {t} c WHERE c.player_id = :s AND {cond})"),
            {"s": survivor, "l": loser})
        conn.execute(sa.text(f"DELETE FROM {t} WHERE player_id = :l"), {"l": loser})

    # 4. Delete the loser, then adopt its yahoo_player_id (now free, unique, real-gsis-derived).
    loser_yahoo = loser_row["yahoo_player_id"]
    conn.execute(sa.text("DELETE FROM players WHERE id = :l"), {"l": loser})
    if loser_yahoo:
        conn.execute(sa.text("UPDATE players SET yahoo_player_id = :y WHERE id = :s"),
                     {"y": loser_yahoo, "s": survivor})


def upgrade() -> None:
    op.create_table(
        "player_merge_audit",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("survivor_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loser_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loser_name", sa.String(100)),
        sa.Column("loser_position", sa.String(5)),
        sa.Column("loser_row", sa.dialects.postgresql.JSONB),
        sa.Column("merged_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    conn = op.get_bind()
    pairs = conn.execute(_ELIGIBLE).fetchall()
    logger.info("merge_dupes: %d eligible two-source group(s) to merge", len(pairs))
    for survivor, loser in pairs:
        if survivor is None or loser is None:
            continue
        _merge_pair(conn, survivor, loser)
    logger.info("merge_dupes: merged %d group(s); held groups untouched", len(pairs))


def downgrade() -> None:
    raise NotImplementedError(
        "A player merge is lossy (rows deleted, FKs repointed) and cannot be reversed "
        "programmatically. Restore from the pre-migration snapshot. The player_merge_audit "
        "table holds the deleted loser rows for forensics."
    )
