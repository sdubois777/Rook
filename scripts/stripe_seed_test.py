"""
Stripe TEST-MODE seeder — creates the Products + Prices this billing slice needs.

Idempotent: every object is looked up by a stable `lookup_key` (Prices) or
metadata marker (Products) before creating, so re-running is safe and never
duplicates. Prints the resulting price ids in .env format for pasting into your
local env.

Creates (amounts/credits from the design doc's tier + credit-pack tables):
  Subscriptions (recurring monthly):
    Rook Intro     $5/mo    -> STRIPE_PRICE_INTRO_MONTHLY
    Rook Standard  $9/mo    -> STRIPE_PRICE_STANDARD_MONTHLY
    Rook Pro       $18/mo   -> STRIPE_PRICE_PRO_MONTHLY
  Credit packs (one-time):
    Small   $5  -> 75 credits   -> STRIPE_PRICE_PACK_SMALL
    Medium  $10 -> 175 credits  -> STRIPE_PRICE_PACK_MEDIUM
    Large   $25 -> 500 credits  -> STRIPE_PRICE_PACK_LARGE

Run (PowerShell), with your TEST secret key in the env:
    $env:STRIPE_SECRET_KEY = "sk_test_..."; uv run python scripts/stripe_seed_test.py

Refuses to run against a live key (must start with 'sk_test_').
"""
from __future__ import annotations

import os
import sys

import stripe

# lookup_key -> (product name, unit_amount cents, recurring?, metadata)
_SUBSCRIPTIONS = [
    ("rook_intro_monthly",    "Rook Intro",    500,  {"tier": "intro"}),
    ("rook_standard_monthly", "Rook Standard", 900,  {"tier": "standard"}),
    ("rook_pro_monthly",      "Rook Pro",      1800, {"tier": "pro"}),
]

# lookup_key -> (product name, unit_amount cents, credits)
_PACKS = [
    ("rook_pack_small",  "Rook Credit Pack — Small",  500,  75),
    ("rook_pack_medium", "Rook Credit Pack — Medium", 1000, 175),
    ("rook_pack_large",  "Rook Credit Pack — Large",  2500, 500),
]

_ENV_VAR = {
    "rook_intro_monthly": "STRIPE_PRICE_INTRO_MONTHLY",
    "rook_standard_monthly": "STRIPE_PRICE_STANDARD_MONTHLY",
    "rook_pro_monthly": "STRIPE_PRICE_PRO_MONTHLY",
    "rook_pack_small": "STRIPE_PRICE_PACK_SMALL",
    "rook_pack_medium": "STRIPE_PRICE_PACK_MEDIUM",
    "rook_pack_large": "STRIPE_PRICE_PACK_LARGE",
}


def _find_price(lookup_key: str):
    existing = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
    return existing.data[0] if existing.data else None


def _find_product(marker: str):
    # Products carry a rook_key marker in metadata; search by it.
    for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        if product.metadata.get("rook_key") == marker:
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

    for lookup_key, name, amount, meta in _SUBSCRIPTIONS:
        product = _ensure_product(lookup_key, name, meta)
        price_id = _ensure_price(
            lookup_key, product.id, amount, recurring=True, metadata=meta
        )
        results[lookup_key] = price_id
        print(f"  {name:<30} {lookup_key:<24} -> {price_id}", file=sys.stderr)

    for lookup_key, name, amount, credits in _PACKS:
        meta = {"credits": str(credits)}
        product = _ensure_product(lookup_key, name, meta)
        price_id = _ensure_price(
            lookup_key, product.id, amount, recurring=False, metadata=meta
        )
        results[lookup_key] = price_id
        print(f"  {name:<30} {lookup_key:<24} -> {price_id}", file=sys.stderr)

    print("\n# ---- paste into your local .env ----")
    for lookup_key, env_var in _ENV_VAR.items():
        print(f"{env_var}={results[lookup_key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
