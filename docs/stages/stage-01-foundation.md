# Stage 1: Project Foundation

## Before starting, read:
- `CLAUDE.md`
- `docs/rules/GIT_RULES.md`
- `docs/ARCHITECTURE.md`

---

## Goal
Working repo, database, and environment. No agents yet.
Every subsequent stage builds on this — get it right.

---

## Tasks

### 1. Repository setup
- Initialize GitHub repo with directory structure from `CLAUDE.md`
- Create `pyproject.toml` with uv, all dependencies listed:
  ```
  anthropic, fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic,
  pgvector, httpx, playwright, apscheduler, python-dotenv, pydantic-settings,
  pytest, pytest-asyncio, pytest-cov, ruff, pre-commit
  ```
- Create `.gitignore` — include `.env`, `__pycache__`, `.pytest_cache`, `node_modules`
- Create `.env.example` with all required keys (see `docs/ARCHITECTURE.md`)
- **ASK USER** for `ANTHROPIC_API_KEY` and any other keys they have available
- Never commit `.env`

### 2. Pre-commit hooks
- Create `.pre-commit-config.yaml` (see `docs/rules/GIT_RULES.md`)
- Run `pre-commit install`
- Verify hooks fire on test commit

### 3. seasons.py utility
- Create `backend/utils/seasons.py` with `get_current_season()`, `get_analysis_seasons()`, `get_analysis_year()`, `get_draft_prep_window()`
- This file must exist before any agent is written
- All agents import from here — never hardcode years

### 4. Database setup
- **ASK USER** to provision Railway Postgres instance OR set up local Postgres
- Set `DATABASE_URL` in `.env`
- Implement `backend/database.py` — async SQLAlchemy engine, session factory, `AsyncSessionLocal`
- Implement `backend/config.py` — pydantic Settings model reading from `.env`

### 5. SQLAlchemy models
Create all models in `backend/models/`:
- `player.py` — master player record (full schema in `docs/ARCHITECTURE.md`)
- `team_system.py` — team system grades
- `player_profile.py` — player profile records
- `player_injury_profile.py` — injury risk records
- `player_schedule.py` — schedule grades
- `player_dependency.py` — dependency flags
- `beat_reporter_signal.py` — beat reporter signals
- `season_roster.py` — in-season roster tracking
- `api_usage_log.py` — API cost tracking (required)
- `agent_cache.py` — input hash + output cache (required)

### 6. Alembic migrations
- Set up Alembic: `alembic init alembic`
- Configure `alembic/env.py` to use async SQLAlchemy engine
- Generate initial migration: `alembic revision --autogenerate -m "initial schema"`
- Run migration: `alembic upgrade head`
- Verify all tables created correctly

### 7. FastAPI app
- Create `backend/main.py` — FastAPI app with health check endpoint
- `GET /health` returns `{"status": "ok", "environment": "development"}`
- Run with `uvicorn backend.main:app --reload`

### 8. Test fixtures
Create all fixture files in `tests/fixtures/` now — needed by all later stages:
- `players.json` — 5 sample player records, different positions/tiers/flags
- `team_systems.json` — 3 samples: strong (elite QB + good line), weak (rookie QB + bad line with compound flag), average
- `draft_state.json` — mid-draft state: 8 players drafted, 3 opponents with varying budgets, one opponent building dangerous roster
- `yahoo_ws_frames.json` — placeholder for now with structure defined. **ASK USER** to capture real Yahoo WS frames when draft room is accessible

### 9. conftest.py
Create `tests/conftest.py` with all four standard mocks:
- `mock_anthropic` — never calls real API
- `mock_db` — no real DB connections
- `mock_nfl_data` — returns fixture dataframes
- `mock_playwright` — no real browser

---

## Required test cases
```python
# tests/unit/test_seasons.py
def test_current_season_before_june_returns_previous_year()
def test_current_season_after_june_returns_current_year()
def test_analysis_seasons_returns_correct_lookback()
def test_analysis_seasons_excludes_current_season()
def test_analysis_year_is_one_ahead_of_current()
def test_get_draft_prep_window_returns_all_fields()

# tests/unit/test_database.py
def test_async_session_created_successfully()
def test_health_check_endpoint_returns_200()
```

---

## Verification before marking complete
1. `GET /health` returns 200
2. All DB tables exist — `alembic upgrade head` runs without errors
3. `pytest tests/unit/ -v` — all green
4. Coverage 80%+ on `backend/utils/seasons.py` and `backend/database.py`
5. Pre-commit hooks fire on test commit
6. `.env` is gitignored and not committed
7. All fixture files exist in `tests/fixtures/`

---

## Commit
```
feat(foundation): initialize project structure, database, and season utilities

All DB tables created via Alembic migration.
seasons.py utility implemented — no hardcoded years anywhere.
Pre-commit hooks installed. All unit tests passing.
```

---

## Ask user
- For `ANTHROPIC_API_KEY` before writing any code that references it
- To provision Railway Postgres OR confirm local Postgres is available
- To confirm `.env` has been created with correct values before running migrations
