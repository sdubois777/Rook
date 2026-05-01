# Fantasy Football AI Platform — Claude Code Entry Point

This file is read automatically at the start of every session.
Read it fully before writing any code.

---

## What This Project Is

Full-season fantasy football management platform powered by AI agents.
The user's league is on **Yahoo Fantasy**, **auction draft format**.

Three phases:
1. **Pre-draft pipeline** — 6 research agents build a structured "draft bible"
2. **Live draft** — Agent controls Yahoo draft room via Playwright, gives real-time recommendations
3. **In-season** — Trade analyzer, lineup optimizer, waiver wire agent

Core philosophy: **never trust third-party projections**. Build valuations from raw data and chain-of-reasoning. The canonical failure case this system exists to prevent: Keenan Allen signing with LAC should have automatically flagged Ladd McConkey's target share as capped. It didn't in 2024. It must in this system.

---

## Mandatory Reading Before Writing Any Code

| Task | Read first |
|------|-----------|
| Any agent | `docs/rules/COST_RULES.md` + `docs/rules/PATTERNS.md` |
| Any agent | `docs/AGENTS.md` for that agent's spec |
| Database work | `docs/SCHEMA.md` |
| Yahoo/Playwright | `docs/LIVE_DRAFT.md` |
| Testing/commits | `docs/rules/GIT_RULES.md` |
| In-season features | `docs/INSEASON.md` |
| Current stage | `docs/stages/stage-XX-name.md` |

---

## Tech Stack (Quick Reference)

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Package manager | uv |
| AI model (reasoning) | `claude-sonnet-4-6` |
| AI model (extraction) | `claude-haiku-4-5-20251001` |
| Database | PostgreSQL 16 + pgvector |
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Backend | FastAPI + WebSockets |
| Task scheduling | APScheduler |
| Yahoo draft control | Playwright |
| Frontend | React + Vite + Tailwind + Zustand |
| Hosting | Railway |
| CI/CD | GitHub Actions |

---

## Model Selection — Non-Negotiable

**Haiku** (`claude-haiku-4-5-20251001`) for:
- Team Systems agent
- Player Profiles agent
- Injury Risk agent
- Schedule agent
- Beat Reporter agent
- Waiver Wire agent
- Any data extraction or formatting task

**Sonnet** (`claude-sonnet-4-6`) for:
- Roster Changes agent (chain-of-reasoning)
- Live Draft agent (real-time decisions)
- Trade Analyzer engine
- Trade Proposal engine
- Opponent Analyzer agent
- Any multi-step causal reasoning task

If you are unsure which to use: **default to Haiku**. Upgrade to Sonnet only if the task requires reasoning through cause-and-effect chains, not just retrieving and formatting data.

---

## Season Year Handling — Critical

**Never hardcode season years.** All agents must derive years dynamically from `backend/utils/seasons.py`:

```python
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

CURRENT_SEASON = get_current_season()        # e.g. 2025
ANALYSIS_SEASONS = get_analysis_seasons(3)   # e.g. [2023, 2024, 2025] — last 3 seasons
ANALYSIS_YEAR = get_analysis_year()          # e.g. 2026 — the draft we're preparing for
```

If you see `CURRENT_SEASON = 2024` or `for season in [2022, 2023, 2024]` anywhere in the codebase, that is a bug. Fix it.

---

## Architecture Rules (Full Detail in docs/rules/PATTERNS.md)

1. **One API call per team** — pre-aggregate all data in Python first, then call the model once
2. **No iterative tool-use loops** in pre-draft pipeline agents — `run_agent()` is only for live draft
3. **No polling** anywhere in the live draft event chain — event-driven only
4. **All agents go through BaseAgent** — never call `client.messages.create()` directly in agent files
5. **Batch by team, never by player** — never loop over players calling the API inside the loop

---

## When to Ask the User

Stop and ask the user before:
- Any step requiring account creation (Railway, Yahoo Developer, GitHub)
- Any OAuth flow that requires browser interaction
- Any step requiring API keys or credentials not already in `.env`
- Running the full pipeline for the first time (show dry-run cost estimate first)
- Any destructive database operation

---

## Repository Structure

```
fantasy-football-ai/
├── CLAUDE.md                    # This file — auto-read by Claude Code
├── docs/
│   ├── ARCHITECTURE.md          # Full system architecture
│   ├── SCHEMA.md                # Complete database schema
│   ├── AGENTS.md                # All 6 pre-draft agent specs
│   ├── LIVE_DRAFT.md            # Yahoo integration + Playwright bridge
│   ├── INSEASON.md              # In-season features spec
│   ├── rules/
│   │   ├── COST_RULES.md        # API cost efficiency — mandatory
│   │   ├── GIT_RULES.md         # Testing and commit workflow — mandatory
│   │   └── PATTERNS.md          # Code patterns with examples — mandatory
│   └── stages/
│       ├── stage-01-foundation.md
│       ├── stage-02-data-ingestion.md
│       ├── stage-03-team-systems.md
│       ├── stage-04-roster-changes.md
│       └── ... (one file per build stage)
├── backend/
│   ├── utils/
│   │   └── seasons.py           # Dynamic season year calculations — always use this
│   ├── agents/
│   ├── engines/
│   ├── integrations/
│   ├── models/
│   ├── routers/
│   └── websocket/
├── frontend/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── scripts/
```

---

## Current Project Status

Update this section as stages complete.

- [x] Stage 1: Foundation
- [x] Stage 2: Data ingestion
- [x] Stage 3: Team Systems agent
- [x] Stage 4: Roster Changes agent
- [x] Stage 5: Player Profiles agent
- [x] Stage 6: Injury Risk agent
- [x] Stage 7: Schedule agent
- [ ] Stage 8: Beat Reporter agent ← current
- [ ] Stage 9: Valuation pass
- [ ] Stage 10: Yahoo API integration
- [ ] Stage 11: Playwright draft bridge
- [ ] Stage 12: Live draft agent
- [ ] Stage 13: Draft UI
- [ ] Stage 14: Season roster store
- [ ] Stage 15–21: In-season features
- [ ] Stage 22: Pipeline admin UI
- [ ] Stage 23: Deployment + testing
