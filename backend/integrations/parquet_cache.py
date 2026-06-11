"""
Shared parquet file-cache helpers.

Each integration module keeps its own cache directory and TTL policy
and delegates the mechanics (path construction, freshness checks,
load-or-fetch) to these functions. Previously this logic was
copy-pasted across nfl_data.py, sleeper.py, and cfb_data.py.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def cache_path(cache_dir: Path, name: str) -> Path:
    """Return the parquet path for a cache entry, creating the directory."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{name}.parquet"


def cache_valid(
    path: Path,
    ttl_hours: float | None,
    min_rows: int | None = None,
) -> bool:
    """True if the cache file exists, is within TTL, and has enough rows.

    ttl_hours=None means the entry never expires (historical data).
    min_rows=None skips the row-count sanity check.
    """
    if not path.exists():
        return False
    if ttl_hours is not None:
        age = (time.time() - path.stat().st_mtime) / 3600
        if age >= ttl_hours:
            return False
    if min_rows is not None:
        try:
            df = pd.read_parquet(path)
        except Exception:
            return False
        if len(df) < min_rows:
            logger.warning(
                "Cache %s has only %d rows (min %d) — re-fetching",
                path.name, len(df), min_rows,
            )
            return False
    return True


def load_or_fetch(
    cache_dir: Path,
    name: str,
    fetch_fn: Callable[[], pd.DataFrame],
    *,
    skip_empty: bool = False,
    fetch_log: str = "Downloading",
) -> pd.DataFrame:
    """Return the cached DataFrame, or fetch and cache it.

    skip_empty=True leaves empty fetch results uncached so the next
    call retries the fetch.
    """
    path = cache_path(cache_dir, name)
    if path.exists():
        logger.debug("Cache hit: %s", name)
        return pd.read_parquet(path)
    logger.info("%s: %s", fetch_log, name)
    df = fetch_fn()
    if skip_empty and df.empty:
        return df
    df.to_parquet(path, index=False)
    return df
