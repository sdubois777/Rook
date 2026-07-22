#!/usr/bin/env python
"""Refresh the local DEV database from a read-only prod dump.

    python scripts/refresh_dev_db.py

One command, idempotent, re-runnable whenever dev drifts. It:
  1. pg_dump PRODUCTION (read-only) via the `rook-dev` container's pg_dump (v16,
     matching the server) into a gitignored host file (devdata/prod.dump).
  2. pg_restore into the DEV database (--clean --if-exists → drops+recreates, so
     re-running is safe). Restores schema + data in one shot (more reliable than
     migrating from the tangled alembic state; the dump carries the alembic stamp).
  3. Prunes the users table on DEV: keep ONLY iamstephen777 (real Clerk id preserved
     so dev auth works), delete every other user CHILD-FIRST across all user-scoped
     tables, and NULL the Stripe columns (dev runs test-mode Stripe — a live customer
     id here is how test billing hits real objects).

SOURCE (prod) is READ-ONLY. TARGET (dev) is verified NON-prod before any restore —
this script refuses to restore ONTO a Railway host.

Prod URL comes from `.env.prod` (loaded via ROOK_ENV_FILE) or --prod-url or the
PROD_DATABASE_URL env var — never from the default .env (which now points at dev).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
DEVDATA = REPO / "devdata"          # gitignored (see .gitignore)
DUMP = DEVDATA / "prod.dump"
CONTAINER = "rook-dev"
KEEP_EMAIL = "iamstephen777@gmail.com"
DEV_DEFAULT = "postgresql://postgres:dev@localhost:5432/rook"  # inside-container target

_PROD_MARKERS = ("rlwy.net", "railway.internal", "railway.app")


def _plain(url: str) -> str:
    """SQLAlchemy → libpq DSN (drop the +asyncpg driver tag)."""
    return url.replace("+asyncpg", "", 1)


def _host(url: str) -> str:
    return (urlparse(_plain(url)).hostname or "").lower()


def _is_prod(url: str) -> bool:
    return any(m in _host(url) for m in _PROD_MARKERS)


def _resolve_prod_url(arg: str | None) -> str:
    if arg:
        return arg
    if os.environ.get("PROD_DATABASE_URL"):
        return os.environ["PROD_DATABASE_URL"]
    # Load .env.prod explicitly (never the dev .env).
    envprod = REPO / ".env.prod"
    if envprod.exists():
        for line in envprod.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("No prod URL: pass --prod-url, set PROD_DATABASE_URL, or add DATABASE_URL to .env.prod")


# Prune everything not belonging to the keep user. FK map (verified against prod):
#   REAL FK to users, ON DELETE CASCADE: draft_sessions, league_auction_history,
#     platform_credentials.
#   NO FK (logical user_id only): user_preferences, credit_usage_log,
#     granted_monthly_invoices, granted_pack_sessions, user_leagues.
#   leagues/league_configs/draft_state are shared reference data (not user-scoped) —
#     left intact.
#
# We delete by the KEEP set, NOT by "existing non-keep users". The old
# `user_id IN (SELECT id FROM gone)` missed TWO orphan classes and reported DONE
# anyway: (1) NULL user_ids never match IN (→ 5 stranded user_preferences), and
# (2) rows whose user_id points at a user already absent in prod — with no FK to
# enforce integrity these exist, and are not in `gone` (→ 6 stranded
# credit_usage_log). `user_id IS DISTINCT FROM <keep_id>` deletes all three: rows
# of the 9 removed users, NULL-user rows, and pre-existing danglers. The CASCADE
# tables are pruned explicitly too — CASCADE only fires on the parent DELETE and
# won't clear rows that already point nowhere.
_USER_TABLES = (
    "user_preferences",
    "credit_usage_log",
    "granted_monthly_invoices",
    "granted_pack_sessions",
    "user_leagues",
    "draft_sessions",
    "league_auction_history",
    "platform_credentials",
)

_DELETES = "\n".join(
    f"DELETE FROM {t} WHERE user_id IS DISTINCT FROM (SELECT id FROM users WHERE email = '{KEEP_EMAIL}');"
    for t in _USER_TABLES
)

# One orphan probe per table, accumulated into `bad`; any nonzero → RAISE EXCEPTION,
# which (inside the txn, under ON_ERROR_STOP=1) rolls back and exits psql non-zero so
# the caller aborts instead of printing DONE. A prune that leaves orphans is not done.
_ORPHAN_CHECKS = "\n".join(
    f"  SELECT count(*) INTO n FROM {t} x WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = x.user_id);\n"
    f"  IF n > 0 THEN bad := bad || format(' {t}=%s', n); END IF;"
    for t in _USER_TABLES
)

_PRUNE_SQL = f"""
BEGIN;
{_DELETES}
DELETE FROM users WHERE email IS DISTINCT FROM '{KEEP_EMAIL}';
UPDATE users SET stripe_customer_id = NULL,
                 stripe_subscription_id = NULL,
                 subscription_status = NULL
 WHERE email = '{KEEP_EMAIL}';
DO $$
DECLARE
  n bigint;
  bad text := '';
  extra_users bigint;
BEGIN
{_ORPHAN_CHECKS}
  SELECT count(*) INTO extra_users FROM users WHERE email IS DISTINCT FROM '{KEEP_EMAIL}';
  IF extra_users > 0 THEN bad := bad || format(' users=%s', extra_users); END IF;
  IF bad <> '' THEN
    RAISE EXCEPTION 'PRUNE FAILED - orphans remain, rolling back:%', bad;
  END IF;
  RAISE NOTICE 'orphan check PASSED: 0 orphans across {len(_USER_TABLES)} user-referencing tables';
END $$;
COMMIT;
"""


def _dx(*args: str, input_bytes: bytes | None = None, capture: bool = False):
    """docker exec into the container. Streams unless capture=True."""
    cmd = ["docker", "exec", "-i", CONTAINER, *args]
    return subprocess.run(cmd, input=input_bytes, capture_output=capture, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh dev DB from a read-only prod dump")
    ap.add_argument("--prod-url", default=None, help="prod DATABASE_URL (else .env.prod / PROD_DATABASE_URL)")
    ap.add_argument("--dev-url", default=DEV_DEFAULT, help="dev DSN as seen INSIDE the container")
    ap.add_argument("--skip-prune", action="store_true", help="restore only, do not prune users")
    args = ap.parse_args()

    prod = _plain(_resolve_prod_url(args.prod_url))
    dev = _plain(args.dev_url)

    # SAFETY: never restore onto prod.
    if _is_prod(dev):
        sys.exit(f"REFUSING: --dev-url host {_host(dev)} looks like PROD. This restore is destructive.")
    if not _is_prod(prod):
        print(f"WARNING: --prod-url host {_host(prod)} is not a Railway host — dumping it anyway.")

    DEVDATA.mkdir(exist_ok=True)
    print(f"[1/3] pg_dump prod ({_host(prod)}) -> {DUMP}  (read-only)")
    with open(DUMP, "wb") as fh:
        subprocess.run(["docker", "exec", CONTAINER, "pg_dump", "-Fc", "--no-owner",
                        "--no-privileges", prod], stdout=fh, check=True)

    print(f"[2/3] pg_restore -> dev ({_host(dev)})  (--clean --if-exists)")
    with open(DUMP, "rb") as fh:
        _dx("pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
            "-d", dev, input_bytes=fh.read())

    if args.skip_prune:
        print("[3/3] prune SKIPPED (--skip-prune)")
    else:
        print(f"[3/3] prune users -> keep {KEEP_EMAIL}, null Stripe cols, verify orphans")
        try:
            _dx("psql", dev, "-v", "ON_ERROR_STOP=1", "-c", _PRUNE_SQL)
        except subprocess.CalledProcessError:
            sys.exit("ABORT: prune failed the orphan assertion (transaction rolled back). "
                     "Dev is NOT refreshed — see the psql error above.")
    print("DONE. Dev refreshed. (Point .env DATABASE_URL at localhost:5433 to use it.)")


if __name__ == "__main__":
    main()
