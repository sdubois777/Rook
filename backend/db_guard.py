"""Host-based prod-write guard — the single source of truth for "am I about to
write to production?".

WHY HOST, NEVER ENVIRONMENT: `settings.environment` has been observed to read
"development" in a process whose DATABASE_URL points at prod (the .env default was
never inverted). Every prod-safety decision therefore keys on the DB *host*, which
cannot lie about where the writes land. Do NOT reintroduce an `environment ==
"production"` check here or in callers.

Prod is identified by the Railway host marker (`rlwy.net`). An explicit, deliberate
override — the env var ROOK_ALLOW_PROD_WRITES=1 — is required to write to prod; it is
impossible to trip by forgetting and easy to set on purpose.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from backend.config import settings

# Railway prod host markers. `rlwy.net` is the Railway proxy domain (e.g.
# switchback.proxy.rlwy.net); `railway` covers the internal *.railway.internal host.
_PROD_HOST_MARKERS = ("rlwy.net", "railway.internal", "railway.app")
PROD_OVERRIDE_ENV = "ROOK_ALLOW_PROD_WRITES"


def db_host(url: str | None = None) -> str:
    """Hostname of the given (or configured) DATABASE_URL, lowercased. Never raises."""
    raw = url if url is not None else settings.database_url
    try:
        return (urlparse(raw.replace("+asyncpg", "", 1)).hostname or "").lower()
    except Exception:
        return ""


def is_prod_db(url: str | None = None) -> bool:
    """True when the DATABASE_URL host is a Railway prod host."""
    host = db_host(url)
    return any(marker in host for marker in _PROD_HOST_MARKERS)


def prod_override_active() -> bool:
    """True only when the operator has explicitly opted into prod writes."""
    return os.environ.get(PROD_OVERRIDE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _banner(operation: str) -> str:
    return (
        "\n" + "=" * 72 + "\n"
        "  [!!] PROD-WRITE GUARD -- REFUSING TO RUN\n"
        + "=" * 72 + "\n"
        f"  Operation : {operation}\n"
        f"  DB host   : {db_host()}   (this is PRODUCTION)\n"
        f"  Env label : settings.environment={settings.environment!r}  "
        "(ignored -- the guard keys on the HOST, not this)\n"
        + "-" * 72 + "\n"
        "  This is a data-mutating operation pointed at the LIVE prod database.\n"
        "  If you did NOT mean to touch prod, you are done -- switch DATABASE_URL to\n"
        "  your dev DB (localhost:5433) and re-run. Forgetting keeps you in dev.\n\n"
        f"  To do this ON PURPOSE, set {PROD_OVERRIDE_ENV}=1 for this one command:\n"
        f"      {PROD_OVERRIDE_ENV}=1 <your command>\n"
        + "=" * 72 + "\n"
    )


def guard_writes(operation: str = "database write") -> None:
    """Refuse a data-mutating operation against prod unless explicitly overridden.

    Call this at the top of every write entrypoint (pipeline, snapshot, migrations,
    seed/recompute/sync scripts). No-op against a dev host. Loud SystemExit against
    prod without the override.
    """
    if is_prod_db() and not prod_override_active():
        sys.stderr.write(_banner(operation))
        raise SystemExit(2)
