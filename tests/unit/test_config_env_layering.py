"""Env-file LAYERING (backend/config.py).

`.env.prod` is a one-line overlay carrying only DATABASE_URL — the secrets live once, in
`.env`. pydantic-settings REPLACES the env file when given a single path, so the previous
`env_file=_ENV_FILE` made `ROOK_ENV_FILE=.env.prod` die at import on the two
required-without-default fields (`anthropic_api_key`, `secret_key`). The documented prod
path could not run at all.

`test_overlay_only_is_missing_the_secrets` is the regression: it reproduces the OLD
single-file behaviour and asserts it still fails, so the layering below is provably load-
bearing rather than incidental.
"""
import pytest
from pydantic import ValidationError

from backend import config, db_guard


PROD_URL = "postgresql+asyncpg://u:p@switchback.proxy.rlwy.net:24364/railway"
DEV_URL = "postgresql+asyncpg://postgres:dev@localhost:5433/rook"

# Set in CI (see .github/workflows/ci.yml) and in a local shell. Process env beats every
# env file in pydantic precedence, so these must be cleared for a file-loading test to
# actually exercise the files.
_OVERRIDING_VARS = ("ANTHROPIC_API_KEY", "SECRET_KEY", "DATABASE_URL")


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    """A `.env` base holding the secrets + dev DB, and a DATABASE_URL-only prod overlay."""
    base = tmp_path / ".env"
    base.write_text(
        "ANTHROPIC_API_KEY=base-anthropic-key\n"
        "SECRET_KEY=base-secret-key\n"
        f"DATABASE_URL={DEV_URL}\n"
    )
    overlay = tmp_path / ".env.prod"
    overlay.write_text(f"DATABASE_URL={PROD_URL}\n")
    for var in _OVERRIDING_VARS:
        monkeypatch.delenv(var, raising=False)
    return str(base), str(overlay)


# --- resolve_env_files: which files, in which order -------------------------------


def test_default_selection_is_the_base_file_alone():
    assert config.resolve_env_files(".env") == (".env",)


def test_selecting_an_overlay_layers_it_after_the_base():
    # Base FIRST (supplies the keys), overlay LAST (wins on DATABASE_URL).
    assert config.resolve_env_files(".env.prod") == (".env", ".env.prod")


def test_base_is_never_duplicated_when_explicitly_selected():
    assert config.resolve_env_files(".env", base=".env") == (".env",)


def test_module_level_env_files_uses_the_resolver():
    assert config._ENV_FILES == config.resolve_env_files(config._ENV_FILE)


# --- The actual load: layering vs replacement -------------------------------------


def test_overlay_only_is_missing_the_secrets(env_files):
    """THE BUG. Replacing the base file (the old `env_file=_ENV_FILE`) leaves the two
    required-without-default fields unset, so Settings() raises at import time."""
    _base, overlay = env_files
    with pytest.raises(ValidationError) as ei:
        config.Settings(_env_file=(overlay,))
    missing = {e["loc"][0] for e in ei.value.errors()}
    assert {"anthropic_api_key", "secret_key"} <= missing


def test_layering_takes_secrets_from_base_and_database_url_from_overlay(env_files):
    base, overlay = env_files
    s = config.Settings(_env_file=config.resolve_env_files(overlay, base=base))
    assert s.anthropic_api_key == "base-anthropic-key"
    assert s.secret_key == "base-secret-key"
    assert s.database_url == PROD_URL


def test_default_selection_resolves_to_the_base_database(env_files):
    base, _overlay = env_files
    s = config.Settings(_env_file=config.resolve_env_files(base, base=base))
    assert s.database_url == DEV_URL
    assert db_guard.is_prod_db(s.database_url) is False


def test_guard_still_keys_on_the_layered_host(env_files):
    """Layering redirects the DB but must not weaken the prod-write guard: the guard reads
    the resolved host, so the overlay is exactly what makes it fire."""
    base, overlay = env_files
    s = config.Settings(_env_file=config.resolve_env_files(overlay, base=base))
    assert db_guard.is_prod_db(s.database_url) is True
    assert db_guard.db_host(s.database_url) == "switchback.proxy.rlwy.net"
