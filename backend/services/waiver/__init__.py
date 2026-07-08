"""Waiver-wire recommendations (v1).

A waiver add/drop is a ONE-SIDED trade: an incoming free-agent add + an outgoing
drop, no counterparty. This package REUSES the trade analyzer's pure lineup
objective (services/trade/lineup.py) rather than building a parallel ranking
path — it imports and composes ``lineup_strength_ppg`` / ``fit_to_limit`` /
``replacement_ppg_by_position`` and never edits them.

v1 is demo-gated (WAIVER_DEMO_MODE) exactly like the trade demo: no active
post-draft leagues exist, so the roster + available pool + waiver settings are
all seeded from real historical data. Teardown mirrors the trade demo.
"""
