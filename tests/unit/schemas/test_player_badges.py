"""The shared PlayerBadgeFields mixin — every player-out schema inherits it so the
badge field is defined ONCE, not copy-pasted. Locks that the field is universal and
defaults to None (healthy → no badge), so surfacing everywhere is a data guarantee."""
from __future__ import annotations

import pytest

from backend.schemas.player_badges import PlayerBadgeFields


def _all_player_out_schemas():
    from backend.routers.players import PlayerDetail, PlayerSummary
    from backend.routers.draftboard import DraftBoardPlayer
    from backend.routers.teams import TeamPlayerSummary
    from backend.routers.league import BiasPlayer
    from backend.routers.trade import LeaguePlayerOut as TradeLeaguePlayerOut
    from backend.routers.trade import PlayerGroundingOut, PlayerRefOut
    from backend.routers.waiver import AddOut, DropOut
    from backend.routers.waiver import LeaguePlayerOut as WaiverLeaguePlayerOut
    from backend.routers.news import SignalFeedItem
    return [
        PlayerSummary, PlayerDetail, DraftBoardPlayer, TeamPlayerSummary, BiasPlayer,
        TradeLeaguePlayerOut, PlayerGroundingOut, PlayerRefOut,
        WaiverLeaguePlayerOut, AddOut, DropOut, SignalFeedItem,
    ]


@pytest.mark.parametrize("schema", _all_player_out_schemas())
def test_every_player_out_schema_inherits_the_mixin(schema):
    # Inherited, NOT copy-pasted — one place to add the next badge field.
    assert issubclass(schema, PlayerBadgeFields)
    assert "injury_status" in schema.model_fields


def test_injury_status_defaults_none_healthy():
    m = PlayerBadgeFields()
    assert m.injury_status is None                 # healthy player → no badge


def test_injury_status_accepts_canonical_code():
    assert PlayerBadgeFields(injury_status="IR").injury_status == "IR"
