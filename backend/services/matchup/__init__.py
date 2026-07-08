"""H2H matchup scouting — pure, deterministic, ZERO-metered primitives.

The Matchup page renders entirely from these (schedule synthesis + lineup/needs
scouting over the SAME evaluate_league values trade/waiver use). No Sonnet, no
credit, no metered-agent path — the only metered call is the explicit user handoff
to the trade Build tab (in the frontend), which this module never touches.
"""
