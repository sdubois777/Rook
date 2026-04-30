# Testing and Git Workflow — Mandatory

A stage is not complete until:
1. Feature code is written and runs without errors
2. Unit tests are written with named test cases
3. All unit tests pass
4. Coverage is 80%+ on all new modules
5. Full unit suite still passes (nothing regressed)
6. Code is committed with a properly formatted commit message
7. Branch is pushed to GitHub
8. CI pipeline passes

Writing the code is not enough. Tests must be written and green.

---

## Unit vs Integration tests

**Unit tests** (`tests/unit/`):
- Mock all external dependencies
- Run fast, cost nothing
- Must pass before every commit
- Never call real Anthropic API, real DB, or real Yahoo API

**Integration tests** (`tests/integration/`):
- Hit real infrastructure
- Run manually before deployment only
- Never run automatically on commit

If a test calls `anthropic.messages.create()` without a mock = integration test.
Put it in `tests/integration/`, not `tests/unit/`.

---

## Named test cases — required

These specific test functions must exist by name. Not optional.

### `test_roster_changes.py`
```
test_mcconkey_allen_displacement        ← THE canonical test
test_target_share_displacement_direct_role_overlap
test_target_share_displacement_no_flag_different_role
test_qb_trust_score_nfl_history
test_qb_trust_score_college_history
test_qb_trust_score_no_history
test_backfield_committee_two_similar_profiles
test_backfield_committee_complementary_profiles
test_dependency_flag_value_impact_applied
test_high_aav_signing_weighted_higher
```

### `test_live_draft.py`
```
test_displaced_flag_activates_when_trigger_drafted
test_displaced_flag_inactive_when_trigger_not_drafted
test_block_flag_fires_on_combo_threat
test_block_flag_suppressed_low_opponent_budget
test_block_flag_suppressed_insufficient_own_budget
test_bid_ceiling_tier1_uses_anchor_weight
test_bid_ceiling_tier4_ignores_anchor
test_recommendation_fires_under_2_seconds
```

### `test_yahoo_playwright.py`
```
test_nomination_event_parsed_from_ws_frame
test_bid_update_event_parsed
test_bridge_failure_emits_manual_action_alert
test_health_check_triggers_reconnect
test_no_polling_in_event_chain
```

### `test_seasons.py`
```
test_current_season_before_june_returns_previous_year
test_current_season_after_june_returns_current_year
test_analysis_seasons_returns_correct_lookback
test_analysis_year_is_one_ahead_of_current
test_no_hardcoded_years_in_agent_files   ← scans codebase for hardcoded years
```

---

## Standard mocks — define in conftest.py

```python
@pytest.fixture
def mock_anthropic():
    with patch("anthropic.AsyncAnthropic") as mock:
        client = AsyncMock()
        mock.return_value = client
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text='{"result": "mocked"}')],
            usage=MagicMock(input_tokens=100, output_tokens=50)
        )
        yield client

@pytest.fixture
def mock_db():
    with patch("backend.database.get_session") as mock:
        session = AsyncMock()
        mock.return_value.__aenter__ = AsyncMock(return_value=session)
        mock.return_value.__aexit__ = AsyncMock(return_value=False)
        yield session

@pytest.fixture
def mock_nfl_data():
    with patch("backend.integrations.nfl_data.NFLDataClient") as mock:
        yield MagicMock()

@pytest.fixture
def mock_playwright():
    with patch("playwright.async_api.async_playwright") as mock:
        yield AsyncMock()
```

---

## Running tests

```bash
# Run unit tests before every commit
pytest tests/unit/ -v

# Run with coverage
pytest tests/unit/ --cov=backend --cov-report=term-missing

# Run specific agent tests
pytest tests/unit/agents/test_roster_changes.py -v

# Run the canonical McConkey/Allen test
pytest tests/unit/agents/test_roster_changes.py::test_mcconkey_allen_displacement -v

# Integration tests — manual only
pytest tests/integration/ -v -m integration
```

Minimum coverage: **80% per module**. Below 80% blocks the commit.

---

## Commit workflow — follow exactly

```bash
# 1. Create feature branch
git checkout -b feat/stage-name

# 2. Write feature code

# 3. Write unit tests

# 4. Run tests — all must pass
pytest tests/unit/ -v

# 5. Check coverage — must be 80%+
pytest tests/unit/ --cov=backend --cov-fail-under=80

# 6. Commit with conventional format
git add .
git commit -m "feat(scope): description

Brief summary of what was built.
Key test cases passing. Coverage: X%."

# 7. Push and open PR to develop
git push origin feat/stage-name
```

---

## Commit message format

```
<type>(<scope>): <short description>
```

**Types:** `feat`, `fix`, `test`, `refactor`, `chore`, `docs`

**Scopes:** `foundation`, `data-ingestion`, `team-systems`, `roster-changes`,
`player-profiles`, `injury-risk`, `schedule`, `beat-reporter`, `valuations`,
`yahoo-api`, `yahoo-playwright`, `live-draft`, `draft-ui`, `season-store`,
`trade-analyzer`, `trade-proposal`, `lineup-optimizer`, `waiver-wire`, `deployment`

**Examples:**
```
feat(roster-changes): add target share displacement model
fix(yahoo-playwright): handle WS reconnect on connection drop
test(injury-risk): add soft tissue pattern detection tests
chore(deps): update anthropic sdk to latest
```

---

## Never do these

```bash
git commit -m "wip"              # No
git commit -m "fix stuff"        # No
git push --force                 # No
git commit --no-verify           # No — never bypass pre-commit hooks
git push origin main             # No — never push directly to main
```

---

## Pre-commit hooks

`.pre-commit-config.yaml` in repo root:

```yaml
repos:
  - repo: local
    hooks:
      - id: run-unit-tests
        name: Unit tests must pass
        entry: pytest tests/unit/ -x -q
        language: system
        pass_filenames: false
        always_run: true

      - id: check-coverage
        name: Coverage must be 80%+
        entry: pytest tests/unit/ --cov=backend --cov-fail-under=80 -q
        language: system
        pass_filenames: false
        always_run: true

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

Install: `pre-commit install`

---

## Branch strategy

```
main        — production, protected, never commit directly
develop     — integration branch
feat/*      — one per build stage
fix/*       — bug fixes
```

---

## GitHub Actions CI

Runs on every push. Blocks merge if unit tests fail or coverage drops below 80%.
See `.github/workflows/ci.yml` in the repo.

**Ask user** to add `ANTHROPIC_API_KEY_TEST` as a GitHub Actions secret
(Settings → Secrets → Actions) — separate key from production with a low spend limit.
