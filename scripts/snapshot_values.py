"""Capture an IMMUTABLE pre-season value snapshot — the locked prediction we later score
against actuals. EXPLICIT command (never pipeline-wired); append-only; idempotent/resumable.

    uv run python scripts/snapshot_values.py --label preseason_2026 --dry-run
    uv run python scripts/snapshot_values.py --label preseason_2026        # real write

⚠️ The real write needs the value_snapshots table (alembic upgrade vs2026snap01) and is a prod
write. Dry-run reads only and writes nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.services.value_snapshot import capture_snapshot  # noqa: E402
from backend.utils.seasons import get_current_season  # noqa: E402


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()[:12]
    except Exception:
        return None


def _print(res: dict) -> None:
    tag = "DRY-RUN (nothing written)" if res["dry_run"] else "WROTE"
    print(f"\n=== value snapshot [{tag}] season={res['season']} label={res['label']} ===")
    print(f"  rows total: {res['rows_total']}   by format: {res['by_format']}")
    print(f"  identity: gsis-resolved={res['gsis_resolved']}  name+position fallback={res['name_fallback']}")
    print(f"  PPR ai_bid_ceiling NULL (must be 0): {res['ppr_ai_bid_ceiling_null']}")
    print(f"  non-PPR PFV-missing (fell back to PPR values): {res['pfv_missing_nonppr']}")
    if not res["dry_run"]:
        print(f"  inserted={res['inserted']}  already-present skipped={res['already_present_skipped']}")
    print("  sample rows:")
    for r in res["sample"]:
        print(f"    {r['player_name']:20s} {r['scoring_format']:9s} "
              f"tier={r['tier']} base=${r['baseline_value']} ai=${r['ai_bid_ceiling']} "
              f"proj={r['projected_ppr']} par={r['par_ratio']} mkt_fp=${r['market_value_fantasypros']} "
              f"| ver={r['valuation_agent_version']}/{r['profiles_prompt_version']} sha={r['git_sha']} "
              f"mkt_src={r['market_source']} mkt_at={r['market_fetched_at']}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Capture an immutable pre-season value snapshot")
    ap.add_argument("--label", required=True, help="snapshot label, e.g. preseason_2026")
    ap.add_argument("--season", type=int, default=None, help="season predicted (default: current)")
    ap.add_argument("--dry-run", action="store_true", help="compute + report, write nothing")
    args = ap.parse_args()
    season = args.season or get_current_season()
    async with AsyncSessionLocal() as s:
        res = await capture_snapshot(s, season, args.label, git_sha=_git_sha(), dry_run=args.dry_run)
    _print(res)


if __name__ == "__main__":
    asyncio.run(main())
