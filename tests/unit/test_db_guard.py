"""Unit tests for the host-based prod-write guard (backend/db_guard.py)."""
import os

import pytest

from backend import db_guard


PROD = "postgresql+asyncpg://u:p@switchback.proxy.rlwy.net:24364/railway"
DEV = "postgresql+asyncpg://postgres:dev@localhost:5433/rook"


def test_db_host_extracts_hostname_without_credentials():
    assert db_guard.db_host(PROD) == "switchback.proxy.rlwy.net"
    assert db_guard.db_host(DEV) == "localhost"
    assert db_guard.db_host("not a url") == ""


def test_is_prod_db_matches_railway_host():
    assert db_guard.is_prod_db(PROD) is True
    assert db_guard.is_prod_db("postgresql://x@y.railway.internal:5432/db") is True
    assert db_guard.is_prod_db(DEV) is False
    assert db_guard.is_prod_db("postgresql://x@localhost/db") is False


def test_is_prod_db_ignores_environment(monkeypatch):
    # Even with environment mislabeled, host is authoritative.
    monkeypatch.setattr(db_guard.settings, "environment", "production")
    assert db_guard.is_prod_db(DEV) is False
    monkeypatch.setattr(db_guard.settings, "environment", "development")
    assert db_guard.is_prod_db(PROD) is True


def test_prod_override_active(monkeypatch):
    monkeypatch.delenv(db_guard.PROD_OVERRIDE_ENV, raising=False)
    assert db_guard.prod_override_active() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv(db_guard.PROD_OVERRIDE_ENV, truthy)
        assert db_guard.prod_override_active() is True
    monkeypatch.setenv(db_guard.PROD_OVERRIDE_ENV, "0")
    assert db_guard.prod_override_active() is False


def test_guard_writes_dev_is_noop(monkeypatch):
    monkeypatch.setattr(db_guard.settings, "database_url", DEV)
    db_guard.guard_writes("test op")  # must not raise


def test_guard_writes_prod_refuses(monkeypatch):
    monkeypatch.setattr(db_guard.settings, "database_url", PROD)
    monkeypatch.delenv(db_guard.PROD_OVERRIDE_ENV, raising=False)
    with pytest.raises(SystemExit) as ei:
        db_guard.guard_writes("test op")
    assert ei.value.code == 2


def test_guard_writes_prod_with_override_allows(monkeypatch):
    monkeypatch.setattr(db_guard.settings, "database_url", PROD)
    monkeypatch.setenv(db_guard.PROD_OVERRIDE_ENV, "1")
    db_guard.guard_writes("test op")  # override present → must not raise
