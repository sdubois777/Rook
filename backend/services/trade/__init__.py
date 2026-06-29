"""In-season trade services: league-state seam + value engine.

This package is the PERMANENT core of the trade feature (per
docs/trade_agent_design.md). It contains no demo/test scaffolding — the demo
seeder and demo league-state source (slice 2) and the analyzer/proposals agents
(slice 3+) live elsewhere and are flag-gated. The agents reason over the
``LeagueState`` seam and the ``InSeasonValue`` bundles produced here, so swapping
the (later) demo source for real in-season data changes nothing in this layer.
"""
