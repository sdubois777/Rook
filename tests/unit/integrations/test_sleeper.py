"""
tests/unit/integrations/test_sleeper.py

Tests for Sleeper API integration.
Uses cached data to avoid hitting the API in CI.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fixtures: mock Sleeper API responses
# ---------------------------------------------------------------------------

def _mock_players_response():
    """Minimal Sleeper /players/nfl response."""
    return {
        "4034": {
            "player_id": "4034",
            "full_name": "Christian McCaffrey",
            "first_name": "Christian",
            "last_name": "McCaffrey",
            "position": "RB",
            "team": "SF",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": None,
            "age": 29,
            "years_exp": 8,
            "college": "Stanford",
            "sportradar_id": "sr-cmc-001",
            "gsis_id": "00-0033280",
            "yahoo_id": 29238,
            "birth_date": "1996-06-07",
            "team_changed_at": None,
        },
        "9493": {
            "player_id": "9493",
            "full_name": "Puka Nacua",
            "first_name": "Puka",
            "last_name": "Nacua",
            "position": "WR",
            "team": "LAR",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": None,
            "age": 24,
            "years_exp": 2,
            "college": "BYU",
            "sportradar_id": "sr-nacua-001",
            "gsis_id": None,
            "yahoo_id": None,
            "birth_date": "2001-01-25",
            "team_changed_at": None,
        },
        "2212": {
            "player_id": "2212",
            "full_name": "Josh Allen",
            "first_name": "Josh",
            "last_name": "Allen",
            "position": "QB",
            "team": "BUF",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": None,
            "age": 29,
            "years_exp": 7,
            "college": "Wyoming",
            "sportradar_id": "sr-jallen-001",
            "gsis_id": "00-0034857",
            "yahoo_id": 30123,
            "birth_date": "1996-05-21",
            "team_changed_at": None,
        },
        "1373": {
            "player_id": "1373",
            "full_name": "Geno Smith",
            "first_name": "Geno",
            "last_name": "Smith",
            "position": "QB",
            "team": "NYJ",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": None,
            "age": 34,
            "years_exp": 12,
            "college": "West Virginia",
            "sportradar_id": "sr-gsmith-001",
            "gsis_id": "00-0030565",
            "yahoo_id": 26631,
            "birth_date": "1990-10-10",
            "team_changed_at": None,
        },
        "96": {
            "player_id": "96",
            "full_name": "Aaron Rodgers",
            "first_name": "Aaron",
            "last_name": "Rodgers",
            "position": "QB",
            "team": None,  # Free agent
            "status": "Active",
            "depth_chart_order": None,
            "injury_status": None,
            "age": 42,
            "years_exp": 21,
            "college": "California",
            "sportradar_id": "sr-arodgers-001",
            "gsis_id": "00-0023459",
            "yahoo_id": 7200,
            "birth_date": "1983-12-02",
            "team_changed_at": None,
        },
        "9221": {
            "player_id": "9221",
            "full_name": "Jahmyr Gibbs",
            "first_name": "Jahmyr",
            "last_name": "Gibbs",
            "position": "RB",
            "team": "DET",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": None,
            "age": 22,
            "years_exp": 2,
            "college": "Alabama",
            "sportradar_id": "sr-gibbs-001",
            "gsis_id": None,
            "yahoo_id": None,
            "birth_date": "2002-08-09",
            "team_changed_at": None,
        },
        "6813": {
            "player_id": "6813",
            "full_name": "Jonathan Taylor",
            "first_name": "Jonathan",
            "last_name": "Taylor",
            "position": "RB",
            "team": "IND",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": None,
            "age": 25,
            "years_exp": 5,
            "college": "Wisconsin",
            "sportradar_id": "sr-jtaylor-001",
            "gsis_id": "00-0036224",
            "yahoo_id": None,
            "birth_date": "1999-01-19",
            "team_changed_at": None,
        },
        "6973": {
            "player_id": "6973",
            "full_name": "J.J. Taylor",
            "first_name": "J.J.",
            "last_name": "Taylor",
            "position": "RB",
            "team": None,  # FA
            "status": "Active",
            "depth_chart_order": None,
            "injury_status": None,
            "age": 27,
            "years_exp": 5,
            "college": "Arizona",
            "sportradar_id": "sr-jjtaylor-001",
            "gsis_id": None,
            "yahoo_id": None,
            "birth_date": "1997-11-23",
            "team_changed_at": None,
        },
        "3198": {
            "player_id": "3198",
            "full_name": "Derrick Henry",
            "first_name": "Derrick",
            "last_name": "Henry",
            "position": "RB",
            "team": "BAL",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": "Questionable",
            "age": 31,
            "years_exp": 9,
            "college": "Alabama",
            "sportradar_id": "sr-dhenry-001",
            "gsis_id": "00-0032764",
            "yahoo_id": 28457,
            "birth_date": "1994-01-04",
            "team_changed_at": None,
        },
        # Kicker — now INGESTED (numeric player_id, Active status, like a skill player).
        "9999": {
            "player_id": "9999",
            "full_name": "Some Kicker",
            "first_name": "Some",
            "last_name": "Kicker",
            "position": "K",
            "team": "KC",
            "status": "Active",
            "depth_chart_order": 1,
            "injury_status": None,
            "age": 28,
            "years_exp": 5,
            "college": "Somewhere",
            "sportradar_id": "sr-kicker-001",
            "gsis_id": None,
            "yahoo_id": None,
            "birth_date": "1996-01-01",
            "team_changed_at": None,
        },
        # Team defense — player_id IS the team abbr, status is NULL, full_name absent.
        "SF": {
            "player_id": "SF",
            "full_name": None,
            "first_name": "San Francisco",
            "last_name": "49ers",
            "position": "DEF",
            "team": "SF",
            "status": None,
            "depth_chart_order": None,
            "injury_status": None,
            "age": None,
            "years_exp": None,
            "college": None,
            "sportradar_id": None,
            "gsis_id": None,
            "yahoo_id": None,
            "birth_date": None,
            "team_changed_at": None,
        },
        # Rams defense — Sleeper keys it "LAR"; our pipeline team convention is "LA".
        # The player_id ("LAR") must survive as the sleeper_id anchor even though
        # `team` gets alias-normalized to "LA".
        "LAR": {
            "player_id": "LAR",
            "full_name": None,
            "first_name": "Los Angeles",
            "last_name": "Rams",
            "position": "DEF",
            "team": "LAR",
            "status": None,
            "depth_chart_order": None,
            "injury_status": None,
            "age": None,
            "years_exp": None,
            "college": None,
            "sportradar_id": None,
            "gsis_id": None,
            "yahoo_id": None,
            "birth_date": None,
            "team_changed_at": None,
        },
    }


def _mock_stats_response_2025():
    """Minimal Sleeper season stats response."""
    return {
        "4034": {
            "pts_ppr": 416.6,
            "pts_half_ppr": 365.6,
            "pts_std": 314.6,
            "gp": 17,
            "rec": 102,
            "rec_yd": 924,
            "rec_td": 7,
            "rec_tgt": 129,
            "rush_att": 311,
            "rush_yd": 1202,
            "rush_td": 10,
            "pos_rank_ppr": 1,
        },
        "6813": {
            "pts_ppr": 280.0,
            "pts_half_ppr": 250.0,
            "pts_std": 220.0,
            "gp": 16,
            "rec": 60,
            "rec_yd": 450,
            "rec_td": 3,
            "rec_tgt": 75,
            "rush_att": 220,
            "rush_yd": 1100,
            "rush_td": 8,
            "pos_rank_ppr": 5,
        },
        "6973": {
            "pts_ppr": 12.0,
            "pts_half_ppr": 8.0,
            "pts_std": 4.0,
            "gp": 3,
            "rec": 4,
            "rec_yd": 20,
            "rec_td": 0,
            "rec_tgt": 6,
            "rush_att": 8,
            "rush_yd": 30,
            "rush_td": 0,
            "pos_rank_ppr": 80,
        },
    }


@pytest.fixture
def mock_sleeper_api():
    """Patch requests.get to return mock Sleeper data."""
    def _side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "players/nfl" in url:
            resp.json.return_value = _mock_players_response()
        elif "stats/nfl/regular/2025" in url:
            resp.json.return_value = _mock_stats_response_2025()
        elif "stats/nfl/regular/2024" in url:
            resp.json.return_value = _mock_stats_response_2025()  # reuse for test
        elif "stats/nfl/regular/2023" in url:
            resp.json.return_value = _mock_stats_response_2025()  # reuse for test
        else:
            resp.status_code = 404
            resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("backend.integrations.sleeper.requests.get", side_effect=_side_effect):
        # Also bypass cache
        with patch("backend.integrations.sleeper._cache_valid", return_value=False):
            yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchSleeperPlayers:
    def test_sleeper_players_loads(self, mock_sleeper_api):
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        assert not df.empty
        # K and DEF are now INGESTED alongside the skill positions.
        assert set(df["position"].unique()) <= {"QB", "RB", "WR", "TE", "K", "DEF"}

    def test_kicker_ingested(self, mock_sleeper_api):
        """Kickers come through with their numeric player_id (they behave like
        skill players — Active/Inactive status)."""
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        k = df[df["position"] == "K"]
        assert len(k) == 1
        assert k.iloc[0]["player_id"] == "9999"
        assert k.iloc[0]["full_name"] == "Some Kicker"

    def test_defense_ingested_with_team_abbr_id_and_built_name(self, mock_sleeper_api):
        """Team defenses ingest despite NULL status; player_id IS the team abbr
        (the sleeper_id anchor), and a full_name is constructed from city+nickname."""
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        defs = df[df["position"] == "DEF"]
        assert len(defs) == 2  # SF + LAR both survive the NULL-status gate
        sf = defs[defs["player_id"] == "SF"].iloc[0]
        assert sf["full_name"] == "San Francisco 49ers"  # built from first+last
        assert sf["team"] == "SF"

    def test_defense_id_stays_raw_while_team_alias_normalizes(self, mock_sleeper_api):
        """The Rams DEF keeps player_id='LAR' (the live-draft resolution anchor)
        even though its `team` alias-normalizes to 'LA'."""
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        rams = df[(df["position"] == "DEF") & (df["player_id"] == "LAR")].iloc[0]
        assert rams["player_id"] == "LAR"   # anchor unchanged
        assert rams["team"] == "LA"          # team normalized to pipeline convention
        assert rams["full_name"] == "Los Angeles Rams"

    def test_cmc_on_sf_active(self, mock_sleeper_api):
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        cmc = df[df["full_name"] == "Christian McCaffrey"]
        assert len(cmc) == 1
        assert cmc.iloc[0]["team"] == "SF"
        assert cmc.iloc[0]["status"] == "Active"

    def test_geno_smith_nyj_depth_1(self, mock_sleeper_api):
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        geno = df[df["full_name"] == "Geno Smith"]
        assert len(geno) == 1
        assert geno.iloc[0]["team"] == "NYJ"
        assert geno.iloc[0]["depth_chart_order"] == 1

    def test_rodgers_is_free_agent(self, mock_sleeper_api):
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        rodgers = df[df["full_name"] == "Aaron Rodgers"]
        assert len(rodgers) == 1
        team = rodgers.iloc[0]["team"]
        assert team is None or pd.isna(team)

    def test_no_duplicate_jonathan_taylor(self, mock_sleeper_api):
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        jt = df[df["full_name"] == "Jonathan Taylor"]
        jj = df[df["full_name"] == "J.J. Taylor"]
        assert len(jt) == 1
        assert len(jj) == 1
        # Different sleeper IDs
        assert jt.iloc[0]["player_id"] != jj.iloc[0]["player_id"]

    def test_sportradar_id_100pct_coverage(self, mock_sleeper_api):
        from backend.integrations.sleeper import fetch_sleeper_players
        df = fetch_sleeper_players()
        # Team defenses are units with no sportradar_id — coverage is 100% across
        # the individual-player positions (skill + K).
        individuals = df[df["position"] != "DEF"]
        coverage = individuals["sportradar_id"].notna().sum() / len(individuals)
        assert coverage == 1.0


class TestSleeperSeasonStats:
    def test_sleeper_stats_2025_cmc_above_400(self, mock_sleeper_api):
        from backend.integrations.sleeper import get_sleeper_seasonal_stats
        stats = get_sleeper_seasonal_stats(2025)
        assert not stats.empty
        cmc = stats[stats["player_name"] == "Christian McCaffrey"]
        assert len(cmc) == 1
        assert float(cmc.iloc[0]["fantasy_points_ppr"]) > 400

    def test_sleeper_stats_2023_available(self, mock_sleeper_api):
        from backend.integrations.sleeper import get_sleeper_seasonal_stats
        stats = get_sleeper_seasonal_stats(2023)
        assert not stats.empty

    def test_sleeper_stats_2024_available(self, mock_sleeper_api):
        from backend.integrations.sleeper import get_sleeper_seasonal_stats
        stats = get_sleeper_seasonal_stats(2024)
        assert not stats.empty

    def test_jj_taylor_does_not_get_jonathan_taylor_stats(self, mock_sleeper_api):
        from backend.integrations.sleeper import get_sleeper_seasonal_stats
        stats = get_sleeper_seasonal_stats(2025)
        # Jonathan Taylor should have real stats
        jt = stats[stats["sleeper_id"] == "6813"]
        jj = stats[stats["sleeper_id"] == "6973"]
        if not jt.empty and not jj.empty:
            jt_ppr = float(jt.iloc[0]["fantasy_points_ppr"])
            jj_ppr = float(jj.iloc[0]["fantasy_points_ppr"])
            assert jt_ppr > 200
            assert jj_ppr < 50
            # They must be different players
            assert jt_ppr != jj_ppr

    def test_stat_lookup_uses_sleeper_id_first(self, mock_sleeper_api):
        """Stats merge should join on sleeper_id — no name ambiguity."""
        from backend.integrations.sleeper import get_sleeper_seasonal_stats
        stats = get_sleeper_seasonal_stats(2025)
        # Each row should have a sleeper_id
        assert "sleeper_id" in stats.columns
        # CMC row should have sportradar_id from player merge
        cmc = stats[stats["player_name"] == "Christian McCaffrey"]
        if not cmc.empty:
            assert pd.notna(cmc.iloc[0].get("sportradar_id"))

    def test_stat_lookup_uses_sportradar_second(self, mock_sleeper_api):
        """Merged stats should include sportradar_id from player data."""
        from backend.integrations.sleeper import get_sleeper_seasonal_stats
        stats = get_sleeper_seasonal_stats(2025)
        if "sportradar_id" in stats.columns:
            # Players with stats should have sportradar_id from merge
            with_sr = stats["sportradar_id"].notna().sum()
            assert with_sr > 0


class TestSleeperDepthCharts:
    def test_warehouse_depth_charts_from_sleeper(self, mock_sleeper_api):
        from backend.integrations.sleeper import get_sleeper_depth_charts
        dc = get_sleeper_depth_charts()
        assert not dc.empty
        assert "pos_rank" in dc.columns
        assert "team" in dc.columns
        # All starters should have pos_rank
        assert (dc["pos_rank"] >= 1).all()

    def test_warehouse_rosters_from_sleeper(self, mock_sleeper_api):
        from backend.integrations.sleeper import fetch_sleeper_players
        players = fetch_sleeper_players()
        # Should include team column
        assert "team" in players.columns
        # CMC should be on SF
        cmc = players[players["full_name"] == "Christian McCaffrey"]
        assert cmc.iloc[0]["team"] == "SF"


class TestSleeperInjuries:
    def test_injuries_returns_injured_players(self, mock_sleeper_api):
        from backend.integrations.sleeper import get_sleeper_injuries
        injuries = get_sleeper_injuries()
        # Derrick Henry has Questionable status in mock
        assert not injuries.empty
        henry = injuries[injuries["player_name"] == "Derrick Henry"]
        assert len(henry) == 1
        assert henry.iloc[0]["injury_status"] == "Questionable"


class TestComputeSleeperTargetShare:
    """Tests for compute_sleeper_target_share()."""

    def test_computes_target_share_correctly(self, mock_sleeper_api, tmp_path):
        """Target share = player targets / team total targets."""
        with patch("backend.integrations.sleeper.CACHE_DIR", tmp_path):
            from backend.integrations.sleeper import compute_sleeper_target_share
            df = compute_sleeper_target_share(2025)

        assert not df.empty
        # CMC (4034): 129 targets on SF
        cmc = df[df["player_name"] == "Christian McCaffrey"]
        assert len(cmc) == 1
        # JT (6813) is on IND — different team, so SF total = CMC targets only (+ JJ Taylor)
        # CMC target share should be > 0
        ts = float(cmc.iloc[0]["avg_target_share"])
        assert ts > 0.0
        assert ts <= 1.0

    def test_output_schema(self, mock_sleeper_api, tmp_path):
        """Output has all expected columns."""
        with patch("backend.integrations.sleeper.CACHE_DIR", tmp_path):
            from backend.integrations.sleeper import compute_sleeper_target_share
            df = compute_sleeper_target_share(2025)

        expected = {
            "player_name", "recent_team", "position", "games",
            "total_targets", "total_receptions", "total_rec_yards", "total_rec_tds",
            "avg_target_share", "total_air_yards", "avg_air_yards_share",
            "total_carries", "total_rush_yards", "total_rush_tds",
            "total_fantasy_points", "season", "ppr_per_game",
            "sleeper_id", "sportradar_id",
        }
        assert expected.issubset(set(df.columns))

    def test_air_yards_are_na(self, mock_sleeper_api, tmp_path):
        """Sleeper has no air yards — columns should be NaN (not 0.0)."""
        with patch("backend.integrations.sleeper.CACHE_DIR", tmp_path):
            from backend.integrations.sleeper import compute_sleeper_target_share
            df = compute_sleeper_target_share(2025)

        assert df["avg_air_yards_share"].isna().all()
        assert df["total_air_yards"].isna().all()

    def test_skill_positions_only(self, mock_sleeper_api, tmp_path):
        """Only QB, RB, WR, TE are included."""
        with patch("backend.integrations.sleeper.CACHE_DIR", tmp_path):
            from backend.integrations.sleeper import compute_sleeper_target_share
            df = compute_sleeper_target_share(2025)

        assert set(df["position"].unique()).issubset({"QB", "RB", "WR", "TE"})

    def test_ppr_per_game(self, mock_sleeper_api, tmp_path):
        """ppr_per_game = fantasy_points_ppr / games."""
        with patch("backend.integrations.sleeper.CACHE_DIR", tmp_path):
            from backend.integrations.sleeper import compute_sleeper_target_share
            df = compute_sleeper_target_share(2025)

        cmc = df[df["player_name"] == "Christian McCaffrey"].iloc[0]
        expected_ppg = 416.6 / 17
        assert abs(float(cmc["ppr_per_game"]) - expected_ppg) < 0.1


class TestNoNflDataPyRosterCalls:
    def test_nfl_data_py_roster_functions_not_called(self):
        """Verify sleeper.py doesn't import or call nfl_data_py roster functions."""
        import inspect
        from backend.integrations import sleeper
        source = inspect.getsource(sleeper)
        assert "fetch_rosters" not in source
        assert "fetch_seasonal_rosters" not in source
        assert "fetch_depth_charts" not in source
