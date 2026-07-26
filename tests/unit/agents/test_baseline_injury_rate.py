"""Injury-shortened seasons in the Python baseline.

The baseline drops seasons under 10 games from the weighted TOTAL, then divided by the
average games of every clean season — including the ones it had just dropped. A short
season therefore lowered the denominator without contributing to the numerator, inflating
points-per-game for precisely the players whose history is hardest to read, and that
inflation multiplied straight into the 17-game projection.
"""
from __future__ import annotations

import importlib

import pytest

pp = importlib.import_module("backend.agents.player_profiles")


def _season(year, games, ppr):
    """A skill season whose counting stats produce `ppr` PPR points in `games` games.

    Expressed purely as receiving yards + receptions so the PPR formula
    (rec + yards*0.1 + tds*6) lands on the target exactly.
    """
    rec = 60.0 * games / 17.0
    yards = (ppr - rec) / 0.1
    return {
        "year": year, "games": games,
        "receptions": rec, "rec_yards": yards, "rec_tds": 0,
        "rush_yards": 0, "rush_tds": 0, "carries": 0,
    }


def _ppg(seasons):
    """Run the real skill-position baseline builder, return implied points per game.

    Divides by the SAME constant the builder scales by, so this measures the per-game
    rate rather than drifting whenever the season-length assumption changes.
    """
    out = pp._compute_clean_baseline(seasons)
    return (out or {}).get("ppr_points", 0.0) / pp.EXPECTED_SEASON_GAMES


def _qb_ppg(seasons):
    """Same, for the QB path."""
    out = pp._compute_qb_baseline(seasons)
    return (out or {}).get("ppr_points", 0.0) / pp.EXPECTED_SEASON_GAMES


def test_short_season_does_not_inflate_the_rate():
    """THE regression. Two full seasons at ~14.7 and ~15.9 PPG plus one 8-game season.
    The answer must sit near the full-season rates, not above both of them."""
    seasons = [
        _season(2022, 17, 250.0),   # 14.7 ppg
        _season(2023, 8, 100.0),    # 12.5 ppg — dropped from the weighted total
        _season(2024, 17, 270.0),   # 15.9 ppg
    ]
    ppg = _ppg(seasons)
    assert 13.0 <= ppg <= 17.0, (
        f"{ppg:.1f} PPG — outside the range of the seasons it was built from; the old "
        "numerator/denominator mismatch returned ~18.8 here"
    )


def test_adding_a_short_season_cannot_raise_the_projection():
    """Monotonicity: appending a WORSE, shorter season must never make a player look
    better. Under the old arithmetic it did."""
    base = [_season(2022, 17, 250.0), _season(2024, 17, 270.0)]
    with_short = [_season(2022, 17, 250.0), _season(2023, 8, 100.0), _season(2024, 17, 270.0)]
    assert _ppg(with_short) <= _ppg(base) + 0.5


def test_a_short_season_at_the_same_rate_barely_moves_it():
    """A half-season at the SAME per-game rate carries the same information about rate,
    so the projection should be close to unchanged."""
    base = [_season(2022, 17, 255.0), _season(2024, 17, 255.0)]      # 15.0 ppg flat
    plus = base + [_season(2023, 8, 120.0)]                          # 15.0 ppg, half year
    assert abs(_ppg(plus) - _ppg(base)) < 2.0


def test_all_seasons_short_still_returns_a_sane_rate():
    """Every season under 10 games — the exclusion empties out and the fallback must not
    divide by an empty set or return zero."""
    seasons = [_season(2022, 6, 90.0), _season(2023, 8, 120.0), _season(2024, 7, 105.0)]
    ppg = _ppg(seasons)
    assert 10.0 <= ppg <= 20.0, ppg


def test_full_seasons_only_is_unchanged_by_the_fix():
    """No injury-shortened season means nothing is excluded, so this path must behave
    exactly as before."""
    seasons = [_season(2022, 17, 200.0), _season(2023, 17, 240.0), _season(2024, 17, 280.0)]
    ppg = _ppg(seasons)
    assert 11.7 <= ppg <= 16.5, ppg


def test_single_short_season_stays_below_the_touch_floor():
    """A 5-game sample is ~18 receptions, under the documented 50-touch minimum, so it
    must return NO projection rather than extrapolating a tiny sample to a full season.
    Rate-scaling makes that floor more important, not less."""
    assert pp._compute_clean_baseline([_season(2024, 5, 80.0)]) == {}


def test_qb_path_also_normalises_for_games():
    """The QB baseline had the mirror defect: the weighted total excluded short seasons
    while the divisor averaged games across all of them, inflating PPG ~22%."""
    def qb(year, games, ppr):
        return {"year": year, "games": games, "fantasy_points_ppr": ppr,
                "passing_yards": 0, "passing_tds": 0, "interceptions": 0,
                "rush_yards": 0, "rush_tds": 0, "receptions": 0}
    ppg = _qb_ppg([qb(2022, 17, 250.0), qb(2023, 8, 100.0), qb(2024, 17, 270.0)])
    assert 13.0 <= ppg <= 17.0, f"{ppg:.1f} PPG — old path returned ~18.8"


def test_projection_scales_to_expected_games_not_a_full_season():
    """Scaling to 17 assumes perfect availability for everyone and was a measured
    regression — projection bias went +14.0 -> +19.2 on the as-of 2025 rebuild.

    availability_pass does NOT compensate: it only prorates a KNOWN current absence and
    touched 5 of 919 valued players on that board, so for the other 914 this constant is
    the only availability adjustment there is.

    14.6 is the mean games played by the top 200 PPR scorers over 2021-2024. The bar
    below is deliberately loose — it pins the INTENT (materially under a full season, and
    not a value tuned to one season's error) rather than the exact figure.
    """
    assert 13.5 <= pp.EXPECTED_SEASON_GAMES <= 15.5, (
        f"{pp.EXPECTED_SEASON_GAMES} is not a plausible expected-games figure"
    )
    assert pp.EXPECTED_SEASON_GAMES < 17, "scaling to a full season over-projects everyone"


def test_a_full_season_player_projects_at_expected_games_not_his_own_17():
    """A player who has played every game still projects at EXPECTED games, because the
    question is how many he will play NEXT season, not how many he played last."""
    seasons = [_season(y, 17, 255.0) for y in (2022, 2023, 2024)]   # 15.0 ppg
    out = pp._compute_clean_baseline(seasons)
    assert out["ppr_points"] < 15.0 * 17, "projected a full 17 games for a healthy player"
    assert out["ppr_points"] == pytest.approx(15.0 * pp.EXPECTED_SEASON_GAMES, rel=0.06)
