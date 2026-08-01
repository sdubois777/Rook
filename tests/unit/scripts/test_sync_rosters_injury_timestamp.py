"""What players.injury_status_updated_at MEANS.

It records when a player's injury status was last CONFIRMED, not when it last
CHANGED. The difference is not cosmetic: the real-league roster builder
(backend/services/trade/real_league_source.py) withholds a stored injury once this
timestamp passes its ceiling, so that a value nobody has re-checked is not presented
as current.

With change-only semantics that test ran backwards. A player who stays injured never
changes status, so the stamp ages while the injury persists — meaning the longest-
running, most certain injuries were the first to be discarded, and a player who
flickers in and out of questionable was always trusted. Measured on the development
database, 90% of stored injuries were being withheld, and 8 of the 9 withheld
injured-reserve players were still injured according to the live feed that same day.

These tests exercise the update callback that scripts/sync_rosters.py hands to the
player repository, which is the only code in the repo that writes this column.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

SYNC_ROSTERS = Path("scripts/sync_rosters.py")


def _on_update_source() -> str:
    """The body of the nested _on_update callback, read from source.

    Read as text on purpose: the callback is defined inside a long async function
    that opens a database session and builds a data warehouse, so calling it would
    mean standing all of that up. What matters here is a structural property — that
    the timestamp write is not nested inside the change check — and that is exactly
    what the source shows.
    """
    tree = ast.parse(SYNC_ROSTERS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_update":
            return ast.unparse(node)
    raise AssertionError("_on_update not found in scripts/sync_rosters.py")


def _timestamp_write_depth() -> int:
    """How many `if` statements enclose the injury timestamp write, within the
    callback. Zero means it happens on every observation."""
    tree = ast.parse(_on_update_source())
    target = "injury_status_updated_at"

    def walk(node, depth):
        best = None
        for child in ast.iter_child_nodes(node):
            child_depth = depth + 1 if isinstance(node, ast.If) and child in node.body else depth
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Attribute) and t.attr == target:
                        best = child_depth if best is None else min(best, child_depth)
            found = walk(child, child_depth)
            if found is not None:
                best = found if best is None else min(best, found)
        return best

    depth = walk(tree, 0)
    assert depth is not None, "no write to injury_status_updated_at found"
    return depth


def test_the_timestamp_is_written_on_every_observation():
    """Not only when the status changes.

    If this write moves back inside the change check, the column silently reverts to
    meaning "last changed" while the freshness ceiling that reads it still assumes
    "last confirmed" — and long-term injuries stop being shown, with nothing failing.
    """
    assert _timestamp_write_depth() == 0


def test_the_status_itself_is_still_only_written_on_change():
    """The guard still exists — it just no longer wraps the timestamp. Removing it
    entirely would rewrite the status on every sweep, which is wasteful and would
    obscure genuine transitions."""
    src = _on_update_source()
    assert "if existing.injury_status != _injury:" in src
    assert "existing.injury_status = _injury" in src


def test_nothing_under_backend_writes_the_column():
    """The application code only READS this timestamp; the sweep script writes it.

    If a second writer appears with different semantics, the freshness ceiling that
    reads it can no longer be reasoned about from one place — which is precisely the
    confusion that produced the defect this file guards.
    """
    writers = []
    for path in Path("backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "injury_status_updated_at" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                # An Attribute target is a write to the column on a model instance.
                # A plain Name target (seen_at = ...) is a local variable holding a
                # value read OUT of it, which is not a write.
                if isinstance(t, ast.Attribute) and t.attr == "injury_status_updated_at":
                    writers.append(f"{path}:{node.lineno}")
    assert writers == [], f"unexpected writer(s) of the column: {writers}"
