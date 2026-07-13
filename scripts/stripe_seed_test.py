"""
Stripe TEST-MODE seeder — creates the Products + Prices this billing slice needs.

Idempotent: every object is looked up by a stable `lookup_key` (Prices) or
metadata marker (Products) before creating, so re-running is safe and never
duplicates. Prints the resulting price ids in .env format for pasting into your
local env.

SINGLE SOURCE OF TRUTH: every dollar amount and credit count is DERIVED from
backend/models/user.py (TIER_LIMITS / CREDIT_PACKS) at run time — nothing is
re-declared here. Creates, per paid tier: a recurring MONTHLY price and a
one-time SEASON price; plus one price per credit pack.

Run (PowerShell), with your TEST secret key in the env:
    $env:STRIPE_SECRET_KEY = "sk_test_..."; uv run python scripts/stripe_seed_test.py

Refuses to run against a live key (must start with 'sk_test_').
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import stripe

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.models.user import CREDIT_PACKS, TIER_LIMITS, TIER_ORDER  # noqa: E402


def _paid_tiers() -> list[str]:
    return [t for t in TIER_ORDER if TIER_LIMITS[t]["price_monthly_usd"] > 0]


def _catalog() -> tuple[list, list]:
    """(subscriptions, one_time) derived from user.py.

    subscriptions: (lookup_key, product name, cents, metadata)
    one_time:      (lookup_key, product name, cents, metadata)
    """
    subs, once = [], []
    for tier in _paid_tiers():
        cfg = TIER_LIMITS[tier]
        label = cfg["label"]
        subs.append((
            f"rook_{tier}_monthly", f"Rook {label} (Monthly)",
            cfg["price_monthly_usd"] * 100, {"tier": tier, "interval": "monthly"},
        ))
        once.append((
            f"rook_{tier}_season", f"Rook {label} (Season Pass)",
            cfg["price_season_usd"] * 100, {"tier": tier, "interval": "season"},
        ))
    for pack, cfg in CREDIT_PACKS.items():
        once.append((
            f"rook_pack_{cfg['credits']}",
            f"Rook Credit Pack — {cfg['credits']} credits",
            cfg["price_usd"] * 100, {"credits": str(cfg["credits"]), "pack": pack},
        ))
    return subs, once


def _env_var(lookup_key: str) -> str:
    # rook_<tier>_<interval> -> STRIPE_PRICE_<TIER>_<INTERVAL>
    # rook_pack_<credits>    -> STRIPE_PRICE_PACK_<CREDITS>
    return "STRIPE_PRICE_" + lookup_key.removeprefix("rook_").upper()


def _find_price(lookup_key: str):
    existing = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
    return existing.data[0] if existing.data else None


def _find_product(marker: str):
    for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        # StripeObject exposes keys as attributes (no dict .get in this SDK).
        metadata = getattr(product, "metadata", None)
        if metadata is not None and getattr(metadata, "rook_key", None) == marker:
            return product
    return None


def _ensure_product(marker: str, name: str, metadata: dict):
    product = _find_product(marker)
    if product:
        return product
    return stripe.Product.create(
        name=name,
        metadata={"rook_key": marker, **metadata},
    )


def _ensure_price(lookup_key: str, product_id: str, amount: int,
                  recurring: bool, metadata: dict) -> str:
    price = _find_price(lookup_key)
    if price:
        return price.id
    kwargs = dict(
        product=product_id,
        unit_amount=amount,
        currency="usd",
        lookup_key=lookup_key,
        metadata=metadata,
    )
    if recurring:
        kwargs["recurring"] = {"interval": "month"}
    return stripe.Price.create(**kwargs).id


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        print("ERROR: STRIPE_SECRET_KEY not set in env.", file=sys.stderr)
        return 1
    if not key.startswith("sk_test_"):
        print(
            "REFUSING: STRIPE_SECRET_KEY is not a test key (sk_test_...). "
            "This seeder is test-mode only.",
            file=sys.stderr,
        )
        return 1

    stripe.api_key = key
    results: dict[str, str] = {}
    subs, once = _catalog()

    for lookup_key, name, amount, meta in subs:
        product = _ensure_product(lookup_key, name, meta)
        price_id = _ensure_price(lookup_key, product.id, amount, True, meta)
        results[lookup_key] = price_id
        print(f"  {name:<34} {lookup_key:<24} ${amount/100:<7.2f} -> {price_id}",
              file=sys.stderr)

    for lookup_key, name, amount, meta in once:
        product = _ensure_product(lookup_key, name, meta)
        price_id = _ensure_price(lookup_key, product.id, amount, False, meta)
        results[lookup_key] = price_id
        print(f"  {name:<34} {lookup_key:<24} ${amount/100:<7.2f} -> {price_id}",
              file=sys.stderr)

    print("\n# ---- paste into your local .env ----")
    for lookup_key, price_id in results.items():
        print(f"{_env_var(lookup_key)}={price_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
