"""Tests for the Sleeper full-state backfill event builder (pure functions).

Payload shapes mirror the REAL public-API responses (verified against captured
mock drafts): picked_by is EMPTY for autopicks/mock bots, so is_yours must fall
back to draft_slot == draft_order[my_user_id].
"""
import json
from pathlib import Path

from backend.services.sleeper_backfill import build_pick_events, my_slot

# Real draft-start capture: a 12-team snake with cpu_autopick=1 where Sleeper sent
# a `player_picked` frame for only 16 of the first 26 picks — the other 10
# (incl. opening 1/2/3) fired ONLY `draft_updated_by_pick` (no identity). REST is
# the authoritative recovery source; this bundle is the /picks response for the
# capture window. See extension/test/fixtures/sleeper/snake-draft-start.json.
_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "extension" / "test" / "fixtures" / "sleeper" / "snake-draft-start-picks.json"
)


def _draft_start_bundle():
    return json.loads(_FIXTURE.read_text())

MY_ID = "1373225184038764544"

SNAKE_DRAFT = {
    "type": "snake",
    "status": "drafting",
    "draft_order": {MY_ID: 4},
    "settings": {"teams": 12, "rounds": 15},
}

AUCTION_DRAFT = {
    "type": "auction",
    "status": "drafting",
    "draft_order": {MY_ID: 3},
    "settings": {"teams": 12, "budget": 200},
}


def _pick(no, slot, first, last, pos="RB", team="DET", picked_by="", amount=None, rnd=1):
    md = {"first_name": first, "last_name": last, "position": pos, "team": team}
    if amount is not None:
        md["amount"] = amount
    return {
        "pick_no": no,
        "round": rnd,
        "draft_slot": slot,
        "picked_by": picked_by,
        "player_id": f"sleeper-{no}",
        "metadata": md,
    }


def test_my_slot_from_draft_order():
    assert my_slot(SNAKE_DRAFT, MY_ID) == 4
    assert my_slot(SNAKE_DRAFT, "someone-else") is None
    assert my_slot(SNAKE_DRAFT, None) is None


def test_snake_events_shape_and_labels():
    picks = [_pick(1, 1, "Jahmyr", "Gibbs"), _pick(2, 2, "Bijan", "Robinson")]
    events = build_pick_events(SNAKE_DRAFT, picks, MY_ID)
    assert [e["type"] for e in events] == ["snake_pick", "snake_pick"]
    p1 = events[0]["payload"]
    assert p1["player_name"] == "Jahmyr Gibbs"
    assert p1["pick_number"] == 1
    assert p1["picker"] == "Team 1"
    assert p1["is_yours"] is False
    assert p1["sleeper_player_id"] == "sleeper-1"


def test_is_yours_by_slot_fallback_when_picked_by_empty():
    # Real mocks: picked_by == "" — the slot fallback is what attributes YOUR pick.
    picks = [_pick(4, 4, "CeeDee", "Lamb", pos="WR", team="DAL")]
    [ev] = build_pick_events(SNAKE_DRAFT, picks, MY_ID)
    assert ev["payload"]["is_yours"] is True
    assert ev["payload"]["picker"] == "You"


def test_is_yours_by_picked_by_when_present():
    picks = [_pick(9, 9, "Puka", "Nacua", picked_by=MY_ID)]  # traded slot: not mine
    [ev] = build_pick_events(SNAKE_DRAFT, picks, MY_ID)
    assert ev["payload"]["is_yours"] is True


def test_events_sorted_by_pick_no():
    picks = [_pick(3, 3, "C", "Three"), _pick(1, 1, "A", "One"), _pick(2, 2, "B", "Two")]
    events = build_pick_events(SNAKE_DRAFT, picks, MY_ID)
    assert [e["payload"]["pick_number"] for e in events] == [1, 2, 3]


def test_auction_events_amount_and_winner():
    picks = [_pick(1, 3, "Ja'Marr", "Chase", pos="WR", team="CIN", amount="72")]
    [ev] = build_pick_events(AUCTION_DRAFT, picks, MY_ID)
    assert ev["type"] == "draft_pick"
    p = ev["payload"]
    assert p["final_price"] == 72          # string amount parsed
    assert p["winner"] == "You"            # slot 3 == my slot
    assert p["is_yours"] is True
    assert p["player_name"] == "Ja'Marr Chase"


def test_auction_missing_amount_defaults_to_zero():
    picks = [_pick(2, 7, "Nico", "Collins", pos="WR", team="HOU")]
    [ev] = build_pick_events(AUCTION_DRAFT, picks, MY_ID)
    assert ev["payload"]["final_price"] == 0
    assert ev["payload"]["winner"] == "Team 7"
    assert ev["payload"]["is_yours"] is False


def test_empty_picks_no_events():
    assert build_pick_events(SNAKE_DRAFT, [], MY_ID) == []
    assert build_pick_events(SNAKE_DRAFT, None, MY_ID) == []


# ── Real draft-start capture: every pick recovered, incl. the no-player_picked gap

def test_draft_start_recovers_all_26_picks_from_rest():
    """The 10 picks Sleeper never sent a player_picked for (1,2,3,5,6,8,14,19,21,23)
    are all present in the REST /picks response and build_pick_events emits every
    one — so reconciliation rosters all 26."""
    bundle = _draft_start_bundle()
    events = build_pick_events(bundle["draft"], bundle["picks"], "1373225184038764544")
    assert len(events) == 26
    assert all(e["type"] == "snake_pick" for e in events)
    nums = [e["payload"]["pick_number"] for e in events]
    assert nums == list(range(1, 27))  # contiguous — no gap
    # The opening autopicks (no player_picked frame) carry full identity from REST.
    by_num = {e["payload"]["pick_number"]: e["payload"] for e in events}
    assert by_num[1]["player_name"] == "Ja'Marr Chase"
    assert by_num[2]["player_name"] == "Christian McCaffrey"
    assert by_num[3]["player_name"] == "Bijan Robinson"
    for n in (1, 2, 3, 5, 6, 8, 14, 19, 21, 23):
        assert by_num[n]["player_name"], f"gap pick {n} unrecovered"
        assert by_num[n]["sleeper_player_id"]  # id-first canonical resolution


def test_draft_start_attribution_my_slot_only():
    """Slot 3 is mine (draft_order maps my user_id → 3). Pick 3 is my own autopick
    (is_yours); the other opening autopicks are NOT mine. This is the trap the
    null-user guard protects — attribution must be by my real slot, not blanket."""
    bundle = _draft_start_bundle()
    events = build_pick_events(bundle["draft"], bundle["picks"], "1373225184038764544")
    by_num = {e["payload"]["pick_number"]: e["payload"] for e in events}
    assert by_num[3]["is_yours"] is True
    assert by_num[3]["picker"] == "You"
    # My second-round pick (serpentine: slot 3 → pick 22) is also mine.
    assert by_num[22]["is_yours"] is True
    # Opening autopicks that aren't my slot must NOT be mine.
    assert by_num[1]["is_yours"] is False
    assert by_num[2]["is_yours"] is False
    mine = [n for n, p in by_num.items() if p["is_yours"]]
    assert mine == [3, 22]  # exactly my two slots in the first 26


def test_draft_start_null_user_attributes_nothing_as_mine():
    """With an unknown user_id, NO pick is is_yours — which is exactly why the
    extension defers this sync (recording it would file my own picks to opponents
    permanently). Documents the backend behavior the guard exists to prevent."""
    bundle = _draft_start_bundle()
    events = build_pick_events(bundle["draft"], bundle["picks"], None)
    assert len(events) == 26
    assert not any(e["payload"]["is_yours"] for e in events)
