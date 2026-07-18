"""identity-corruption cleanup (deterministic classes A/B/C/D)

One-time PROD data migration cleaning identity corruption left from the two-source
ingestion era. Deterministic classes ONLY — the manual-review subset (Zach Miller and
other genuinely-distinct or both-distinct-sleeper pairs, the one FK-referenced junk row)
is UNTOUCHED.

  A — crossed-id fix: a row whose yahoo_player_id encodes a DIFFERENT gsis than it owns
      (Tyler Conklin carrying Ryan Izzo's id). Corrected to nfl_<own gsis>. Rows stay
      SEPARATE (id-fix, NOT a merge) — distinct humans are never fused. Guarded on the
      target id being free.
  B — whitespace gsis: gsis_id with leading/trailing space, trimmed — but ONLY where the
      trim does NOT collide with an existing clean row (colliders are same-human dup pairs,
      handled by C instead).
  C — same-human dup merge (reuses #315's _merge_pair + player_merge_audit): pairs that
      share a REAL gsis with <=1 distinct sleeper_id, OR share a sleeper_id (same Sleeper
      person). Both-distinct-sleeper pairs (Matt Colburn / Ronald Jones / Joe Horn / Reggie
      Davis) are EXCLUDED to manual review. Survivor keeps the active identity; the loser's
      fields (incl. valuation) are unioned in, FK children repointed, then it is deleted.
  D — junk delete: 'Player Invalid' / 'Duplicate Player' rows with NO FK references. Any
      junk row that IS FK-referenced is HELD + skipped (recorded, not deleted).

Class E (null-gsis backfill) is a no-op here (those resolve by sleeper_id; sync handles gsis).

Idempotent (a clean DB → no eligible rows → no-op). Atomic (one transaction). Every change
recorded: merges in player_merge_audit, id-fixes/trims/deletes in player_cleanup_audit.
BACKOUT: lossy (rows deleted/repointed) — authoritative backout is the pre-migration
snapshot; the audit tables hold the before-state JSON.

Revision ID: e2f3a4b5c6d7
Revises: d1a2b3c4e5f7
"""
import json
import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Boolean

from backend.models.player import Player

revision = "e2f3a4b5c6d7"
down_revision = "d1a2b3c4e5f7"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.identity_cleanup")

_JUNK_NAMES = ("Player Invalid", "Duplicate Player")

# Every FK column that references players.id — for the junk-delete reference check + merge repoint.
_FK_REFS = [
    ("beat_reporter_signals", "player_id"),
    ("league_auction_history", "player_id"),
    ("market_value_historic", "player_id"),
    ("player_dependencies", "player_id"),
    ("player_dependencies", "trigger_player_id"),
    ("player_format_values", "player_id"),
    ("player_injury_profiles", "player_id"),
    ("player_profiles", "player_id"),
    ("player_schedules", "player_id"),
    ("season_roster", "player_id"),
]

# --- #315 merge machinery (copied verbatim so the migration is self-contained) ----------
_ONE_TO_ONE = ("player_profiles", "player_injury_profiles", "player_schedules")
_UNIQUE_KEYED = {
    "league_auction_history": ["season_year", "source"],
    "market_value_historic": ["season_year"],
    "player_format_values": ["scoring_format"],
}


def _merge_pair(conn, survivor, loser) -> None:
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
    for t in _ONE_TO_ONE:
        conn.execute(sa.text(
            f"UPDATE {t} SET player_id = :s WHERE player_id = :l "
            f"AND NOT EXISTS (SELECT 1 FROM {t} c WHERE c.player_id = :s)"),
            {"s": survivor, "l": loser})
        conn.execute(sa.text(f"DELETE FROM {t} WHERE player_id = :l"), {"l": loser})
    conn.execute(sa.text("UPDATE player_dependencies SET player_id = :s WHERE player_id = :l"),
                 {"s": survivor, "l": loser})
    conn.execute(sa.text("UPDATE player_dependencies SET trigger_player_id = :s WHERE trigger_player_id = :l"),
                 {"s": survivor, "l": loser})
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
    loser_yahoo = loser_row["yahoo_player_id"]
    conn.execute(sa.text("DELETE FROM players WHERE id = :l"), {"l": loser})
    if loser_yahoo:
        conn.execute(sa.text(
            "UPDATE players SET yahoo_player_id = :y WHERE id = :s "
            "AND NOT EXISTS (SELECT 1 FROM players p WHERE p.yahoo_player_id = :y AND p.id <> :s)"),
            {"y": loser_yahoo, "s": survivor})


def _referenced(conn, pid) -> bool:
    for t, c in _FK_REFS:
        n = conn.execute(sa.text(f"SELECT 1 FROM {t} WHERE {c} = :i LIMIT 1"), {"i": pid}).first()
        if n:
            return True
    return False


def _cleanup_audit(conn, player_id, action, detail):
    conn.execute(
        sa.text("""INSERT INTO player_cleanup_audit (player_id, action, detail)
                   VALUES (:pid, :act, CAST(:d AS jsonb))"""),
        {"pid": player_id, "act": action, "d": json.dumps(detail)},
    )


def _pick(rows):
    """Survivor = the row keeping the active identity: prefer valid team, then valued,
    then a sleeper_id, then the smallest id (deterministic). Loser = the other. Valuation
    is preserved regardless via the per-field COALESCE union in _merge_pair."""
    def score(r):
        team_ok = 1 if (r["team_abbr"] and r["team_abbr"] != "FA") else 0
        return (team_ok, 1 if r["valued"] else 0, 1 if r["sleeper_id"] else 0, str(r["id"]))
    ordered = sorted(rows, key=score, reverse=True)
    return ordered[0]["id"], ordered[1]["id"]


def upgrade() -> None:
    op.create_table(
        "player_cleanup_audit",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("detail", sa.dialects.postgresql.JSONB),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    conn = op.get_bind()

    # --- Class A: crossed-id fix (yahoo encodes a different gsis than the row owns) -------
    crossed = conn.execute(sa.text(
        "SELECT id, gsis_id, yahoo_player_id FROM players "
        "WHERE yahoo_player_id LIKE 'nfl_00-%' AND gsis_id IS NOT NULL "
        "AND btrim(yahoo_player_id) <> ('nfl_' || btrim(gsis_id))")).fetchall()
    a_fixed = 0
    for pid, gsis, ya in crossed:
        target = "nfl_" + gsis.strip()
        taken = conn.execute(sa.text(
            "SELECT 1 FROM players WHERE yahoo_player_id = :t AND id <> :i LIMIT 1"),
            {"t": target, "i": pid}).first()
        if taken:
            logger.warning("identity_cleanup: crossed-id target %s taken — holding %s", target, pid)
            continue
        _cleanup_audit(conn, pid, "crossed_id_fix", {"old": ya, "new": target})
        conn.execute(sa.text("UPDATE players SET yahoo_player_id = :t WHERE id = :i"),
                     {"t": target, "i": pid})
        a_fixed += 1

    # --- Class B: trim whitespace gsis, skipping trims that would collide with a clean row -
    dirty = conn.execute(sa.text(
        "SELECT id, gsis_id FROM players WHERE gsis_id IS NOT NULL AND gsis_id <> btrim(gsis_id)")).fetchall()
    b_trimmed = 0
    for pid, gsis in dirty:
        clean = gsis.strip()
        collide = conn.execute(sa.text(
            "SELECT 1 FROM players WHERE gsis_id = :c AND gsis_id = btrim(gsis_id) AND id <> :i LIMIT 1"),
            {"c": clean, "i": pid}).first()
        if collide:
            continue  # same-human dup pair — handled by Class C, not a blind trim
        _cleanup_audit(conn, pid, "gsis_trim", {"old": gsis, "new": clean})
        conn.execute(sa.text("UPDATE players SET gsis_id = :c WHERE id = :i"), {"c": clean, "i": pid})
        b_trimmed += 1

    # --- Class C: merge same-human dup pairs ---------------------------------------------
    junk_list = "('" + "','".join(_JUNK_NAMES) + "')"
    pair_keys = []
    seen = set()

    def _collect(group_col, having_extra=""):
        vals = conn.execute(sa.text(
            f"SELECT btrim({group_col}) v FROM players "
            f"WHERE {group_col} IS NOT NULL AND btrim({group_col}) <> '' AND name NOT IN {junk_list} "
            f"GROUP BY btrim({group_col}) HAVING count(*) = 2 {having_extra}")).fetchall()
        for (v,) in vals:
            rows = conn.execute(sa.text(
                f"SELECT id, sleeper_id, team_abbr, "
                f"(recommended_bid_ceiling IS NOT NULL OR adp_fantasypros IS NOT NULL) AS valued "
                f"FROM players WHERE btrim({group_col}) = :v"), {"v": v}).mappings().all()
            if len(rows) != 2:
                continue
            s, l = _pick(rows)
            key = frozenset({s, l})
            if key in seen:
                continue
            seen.add(key)
            pair_keys.append((s, l))

    # (a) share a REAL gsis with <=1 distinct sleeper_id (excludes both-distinct-sleeper)
    _collect("gsis_id", "AND btrim(gsis_id) LIKE '00-%' AND count(DISTINCT btrim(sleeper_id)) <= 1")
    # (b) share a sleeper_id (same Sleeper person — catches Gee Scott/Jr; dedups Taysom/McGough)
    _collect("sleeper_id")

    c_merged = 0
    for s, l in pair_keys:
        both = conn.execute(sa.text(
            "SELECT count(*) FROM players WHERE id IN (:s, :l)"), {"s": s, "l": l}).scalar()
        if both != 2:
            continue  # already merged on a prior run / one side gone — idempotent skip
        _merge_pair(conn, s, l)
        c_merged += 1

    # --- Class D: delete unreferenced junk rows -----------------------------------------
    junk = conn.execute(sa.text(
        f"SELECT * FROM players WHERE name IN {junk_list}")).mappings().all()
    d_deleted = d_held = 0
    for row in junk:
        pid = row["id"]
        if _referenced(conn, pid):
            _cleanup_audit(conn, pid, "junk_held_referenced",
                           {k: str(v) for k, v in dict(row).items()})
            d_held += 1
            continue
        _cleanup_audit(conn, pid, "junk_delete", {k: str(v) for k, v in dict(row).items()})
        conn.execute(sa.text("DELETE FROM players WHERE id = :i"), {"i": pid})
        d_deleted += 1

    logger.info(
        "identity_cleanup: A crossed-id fixed=%d | B gsis trimmed=%d | C merged=%d | "
        "D junk deleted=%d, held(referenced)=%d",
        a_fixed, b_trimmed, c_merged, d_deleted, d_held,
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Identity cleanup is lossy (rows merged/deleted, ids rewritten). Restore from the "
        "pre-migration snapshot. Before-state is in player_merge_audit (merges) and "
        "player_cleanup_audit (id-fixes, trims, junk deletes)."
    )
