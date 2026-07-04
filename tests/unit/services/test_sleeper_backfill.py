"""Tests for the Sleeper full-state backfill event builder (pure functions).

Payload shapes mirror the REAL public-API responses (verified against captured
mock drafts): picked_by is EMPTY for autopicks/mock bots, so is_yours must fall
back to draft_slot == draft_order[my_user_id].
"""
from backend.services.sleeper_backfill import build_pick_events, my_slot

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
