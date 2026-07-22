#!/usr/bin/env python
"""Read-only pg_dump of PROD to a local file (restore point). Password passed via
env (PGPASSWORD), never in argv. Usage: prod_snapshot.py <out.dump>"""
import os, re, subprocess, sys
from pathlib import Path
from urllib.parse import urlparse


def prod_url() -> str:
    if os.environ.get("PROD_DATABASE_URL"):
        return os.environ["PROD_DATABASE_URL"]
    for l in Path(".env.prod").read_text().splitlines():
        if l.strip().startswith("DATABASE_URL="):
            return l.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no prod url")


u = urlparse(re.sub(r"\+asyncpg", "", prod_url()))
host, port, user, pw, db = u.hostname, str(u.port or 5432), u.username, u.password, u.path.lstrip("/")
assert host and ("rlwy.net" in host or "railway" in host), f"not prod host: {host}"
out = sys.argv[1]
env = dict(os.environ, PGPASSWORD=pw)
with open(out, "wb") as f:
    r = subprocess.run(
        ["docker", "exec", "-i", "-e", "PGPASSWORD", "rook-dev",
         "pg_dump", "-h", host, "-p", port, "-U", user, "-d", db, "-Fc", "--no-owner"],
        env=env, stdout=f)
print("host:", re.sub(r"\..*", "", host), "db:", db, "rc:", r.returncode)
sys.exit(r.returncode)
