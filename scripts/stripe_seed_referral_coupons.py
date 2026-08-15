"""
Stripe COUPON seeder — creates the coupons the referral program redeems.

Companion to scripts/stripe_seed_test.py, which creates Products and Prices.
This script creates Coupons and nothing else:

  duration=once      welcome discount   — emailed to a free signup who has not paid
  duration=once      referred discount  — a new customer checking out with someone
                                          else's referral code
  duration=forever   referrer reward    — one coupon per rate the program can reach
                                          (the referrer's earned rate is applied as
                                          ONE coupon at the summed rate, never as
                                          several stacked coupons)

SINGLE SOURCE OF TRUTH: every percentage is DERIVED from REFERRAL_PROGRAM in
backend/models/user.py at run time. No percentage is written literally here.

Idempotent: each coupon is created with an explicit deterministic id, retrieved
first, and skipped when it already exists. Re-running reports "exists" and
changes nothing. Stripe coupons are immutable in the fields that matter
(percent_off, duration, applies_to), so a rate change means a NEW coupon id, not
an edit — which the id scheme gives for free.

Every coupon is RESTRICTED to the monthly products (applies_to.products), and
that restriction is a precondition, not a preference. If the monthly product ids
cannot be resolved, the script creates nothing and exits non-zero. An
unrestricted coupon discounts the season products and the credit packs too, and
Stripe cannot add applies_to to a coupon that already exists — so an
unrestricted coupon is permanent, and a re-run cannot repair it. The unsafe path
exists behind --allow-unrestricted and nowhere else.

Run (PowerShell), with the secret key for the mode you are seeding in the env:

    $env:STRIPE_SECRET_KEY = "sk_test_..."
    uv run python scripts/stripe_seed_referral_coupons.py --dry-run
    uv run python scripts/stripe_seed_referral_coupons.py

    --dry-run              resolve the restriction, print exactly what would be
                           created and which products it would be pinned to, and
                           create nothing
    --allow-live           required before a live key is accepted
    --allow-unrestricted   create the coupons with NO product restriction when
                           the monthly products cannot be resolved. Permanent
                           and irreversible — see above

--dry-run reads Stripe (Price lookups) so it can name the products. It writes
nothing, and its exit code is the exit code the real run would give.

Coupons are per Stripe MODE. The objects created with a test key do not exist in
live mode, so this must be run once against every mode in use.

The secret key is never printed or logged — only the one-word mode it implies.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import stripe

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings  # noqa: E402
from backend.models.user import (  # noqa: E402
    REFERRAL_PROGRAM,
    referral_percent_tiers,
)

# The MONTHLY price ids, by config field. Season passes are deliberately absent:
# the referral program is monthly-only (REFERRAL_PROGRAM["eligible_intervals"]),
# and restricting the coupons to the monthly products is the second line of
# defence behind the API layer, which already refuses a season checkout.
MONTHLY_PRICE_ATTRS = ("stripe_price_standard_monthly", "stripe_price_pro_monthly")

DURATION_ONCE = "once"
DURATION_FOREVER = "forever"


def coupon_id(duration: str, percent_off: int) -> str:
    """Deterministic coupon id — "rook_once_20", "rook_forever_30", and so on.

    RUNTIME COUNTERPART: backend/services/billing/catalog.py builds the same ids
    when it attaches a coupon to a checkout session or a subscription. THE TWO
    MUST NOT DRIFT. If the runtime builds an id this seeder never created,
    Stripe rejects the coupon and the discount silently does not apply — the
    customer pays full price and nothing errors on our side. Change one, change
    both.
    """
    return f"rook_{duration}_{percent_off}"


def coupon_plan() -> list[dict]:
    """Every coupon this script is responsible for, in creation order.

    Keyed by id so that two roles landing on the same rate (welcome and referred
    are separate settings and may be set to the same percentage) produce ONE
    coupon carrying both role names, not two attempts at the same id.
    """
    roles: list[tuple[str, int, str]] = [
        ("welcome", REFERRAL_PROGRAM["welcome_percent_off"], DURATION_ONCE),
        ("referred", REFERRAL_PROGRAM["referred_percent_off"], DURATION_ONCE),
    ]
    roles += [("referrer", pct, DURATION_FOREVER) for pct in referral_percent_tiers()]

    plan: dict[str, dict] = {}
    for role, percent_off, duration in roles:
        cid = coupon_id(duration, percent_off)
        entry = plan.get(cid)
        if entry is None:
            plan[cid] = {
                "id": cid,
                "percent_off": percent_off,
                "duration": duration,
                "roles": [role],
            }
        elif role not in entry["roles"]:
            entry["roles"].append(role)

    for entry in plan.values():
        roles_label = "/".join(entry["roles"])
        entry["name"] = (
            f"Rook {roles_label} — {entry['percent_off']}% off "
            f"({'first month' if entry['duration'] == DURATION_ONCE else 'recurring'})"
        )
        # Metadata is how support and the billing dashboard tell these apart
        # later. Stripe stores metadata values as strings.
        entry["metadata"] = {
            "rook_roles": roles_label,
            "percent_off": str(entry["percent_off"]),
            "duration": entry["duration"],
        }
    return list(plan.values())


def key_mode(key: str) -> str:
    """"test", "live", or "unknown" — derived from the key PREFIX only.

    This is the only thing about the key that is ever printed. Restricted keys
    (rk_) carry the same mode marker as secret keys (sk_).
    """
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    if key.startswith(("sk_live_", "rk_live_")):
        return "live"
    return "unknown"


def monthly_product_ids(key: str) -> tuple[list[str], list[str]]:
    """(product ids behind the configured monthly prices, problems).

    A coupon restricted with applies_to.products can only discount line items
    for those products, so this pins the referral coupons to the monthly plans.

    `problems` names each monthly price id that could not be turned into a
    product id, and is empty on success. When it is non-empty the product list
    is empty too: one unusable id drops the restriction ENTIRELY rather than
    producing a partial one, because pinning to whichever tier happens to be
    configured would stop the other tier's monthly checkout from taking the
    discount at all.

    Two ways a price id goes unusable, and both happen in practice:
      * not set — production reads injected environment variables and no .env
        file, so a variable that exists only in .env.example is unset there;
      * no such price in this mode — test-mode price ids do not resolve under a
        live key.

    This function decides nothing. It reports, and main() refuses.
    """
    product_ids: list[str] = []
    problems: list[str] = []
    for attr in MONTHLY_PRICE_ATTRS:
        price_id = getattr(settings, attr, None)
        if not price_id:
            problems.append(f"{attr.upper()} (not set)")
            continue
        try:
            price = stripe.Price.retrieve(price_id, api_key=key)
        except stripe.InvalidRequestError:
            problems.append(f"{attr.upper()} (no such price in this mode)")
            continue
        product = price["product"]
        # `product` is an id string unless the caller expanded it.
        product_ids.append(product if isinstance(product, str) else product["id"])

    if problems:
        return [], problems

    # Both monthly prices could in principle share one product; Stripe rejects a
    # duplicated id in applies_to.
    deduped = list(dict.fromkeys(product_ids))
    return deduped, []


def restriction_label(product_ids: list[str]) -> str:
    """What the coupons are pinned to, named exactly, for the operator to read.

    Plain ASCII: it prints to a Windows console whose code page mangles
    anything else, and this is the line that tells the operator the coupons are
    not restricted. The same goes for the refusal message below.
    """
    if product_ids:
        return ", ".join(product_ids)
    return "UNRESTRICTED (any product, including season passes and credit packs)"


def unresolved_restriction_message(problems: list[str]) -> str:
    """Why the run stopped, and what the operator has to do about it.

    Spelled out at length because the failure it replaces was silent: the script
    used to print a warning, create unrestricted coupons and exit 0, so the
    operator had no reason to look.
    """
    cap = REFERRAL_PROGRAM["referrer_percent_off_cap"]
    return "\n".join([
        "REFUSING: the monthly products could not be resolved: "
        + ", ".join(problems)
        + ".",
        "NOTHING WAS CREATED.",
        "",
        "Creating the coupons now would leave them with no applies_to "
        "restriction. An unrestricted coupon discounts ANY product, the season "
        f"passes and the credit packs included, at up to {cap}% off, and the "
        "referrer coupons carry that rate for the life of the subscription.",
        "",
        "Stripe cannot add applies_to to a coupon that already exists, so a "
        "later run cannot repair it. The coupons would have to be deleted and "
        "recreated, and any customer already holding one keeps the unrestricted "
        "terms.",
        "",
        "Fix the price ids and re-run. In production they come from injected "
        "environment variables and no .env file is read, so a variable that "
        "exists only in .env.example is unset there.",
        "",
        "If unrestricted coupons are genuinely what you want, re-run with "
        "--allow-unrestricted.",
    ])


def find_coupon(cid: str, key: str):
    """The existing coupon with this id, or None."""
    try:
        return stripe.Coupon.retrieve(cid, api_key=key)
    except stripe.InvalidRequestError as exc:
        # "resource_missing" is the only invalid-request outcome that means
        # "not created yet". Anything else (a malformed id, a permissions
        # problem) is a real failure and must not be swallowed into a create.
        if getattr(exc, "code", None) not in (None, "resource_missing"):
            raise
        return None


def ensure_coupon(entry: dict, product_ids: list[str], key: str) -> tuple[str, str]:
    """(coupon id, "exists" | "created"). Never edits an existing coupon.

    An empty `product_ids` creates the coupon unrestricted. main() reaches that
    only under --allow-unrestricted; do not call this with an empty list to work
    around a resolution failure, because the result cannot be corrected later.
    """
    existing = find_coupon(entry["id"], key)
    if existing is not None:
        return entry["id"], "exists"

    kwargs = dict(
        api_key=key,
        id=entry["id"],
        percent_off=entry["percent_off"],
        duration=entry["duration"],
        name=entry["name"],
        metadata=entry["metadata"],
    )
    if product_ids:
        kwargs["applies_to"] = {"products": product_ids}
    created = stripe.Coupon.create(**kwargs)
    return created["id"], "created"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the Rook referral-program Stripe coupons (idempotent).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "print exactly what would be created and which products it would be "
            "restricted to; reads Stripe prices, writes nothing"
        ),
    )
    parser.add_argument(
        "--allow-live", action="store_true",
        help="permit a live secret key (refused by default)",
    )
    parser.add_argument(
        "--allow-unrestricted", action="store_true",
        help=(
            "create the coupons with NO product restriction when the monthly "
            "products cannot be resolved. Permanent: Stripe cannot add the "
            "restriction to an existing coupon. Without this flag an "
            "unresolvable price id stops the run and creates nothing"
        ),
    )
    return parser.parse_args(argv)


def _print_mode_reminder(mode: str) -> None:
    """The one thing that is easy to get wrong: coupons do not cross modes."""
    if mode in ("test", "live"):
        other = "live" if mode == "test" else "test"
        tail = (
            f"These ids exist only in {mode} mode — they do NOT appear in "
            f"{other} mode."
        )
    else:
        tail = "A coupon created with a test key does NOT exist in live mode."
    print(
        f"\nREMINDER: Stripe coupons are per MODE. {tail} Run this script once "
        "against every mode in use."
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = coupon_plan()

    # os.environ first so a one-off `$env:STRIPE_SECRET_KEY = ...` beats whatever
    # is configured, matching scripts/stripe_seed_test.py.
    key = os.environ.get("STRIPE_SECRET_KEY", "") or (settings.stripe_secret_key or "")
    mode = key_mode(key)

    # The key checks cover --dry-run too. A dry run resolves the product
    # restriction against Stripe so it can name the products, and a preview that
    # cannot answer that question is not a preview of anything.
    if not key:
        print(
            "ERROR: STRIPE_SECRET_KEY not set in env. --dry-run needs it too: "
            "the products a coupon would be restricted to are read from Stripe.",
            file=sys.stderr,
        )
        return 1
    if mode == "unknown":
        print(
            "REFUSING: STRIPE_SECRET_KEY is not recognisable as a test or live "
            "key (expected an sk_/rk_ prefix). Coupons are per mode, so a key "
            "whose mode cannot be read is not safe to seed with.",
            file=sys.stderr,
        )
        return 1
    if mode == "live" and not args.allow_live:
        if not args.dry_run:
            print(
                "REFUSING: that is a LIVE key. Coupons created here are real "
                "discounts against real money. Re-run with --allow-live if that "
                "is what you intend.",
                file=sys.stderr,
            )
            return 1
        # A dry run only reads, so it is allowed to look at live mode. Say what
        # the real run will do, so the preview is not mistaken for a green light.
        print(
            "NOTE: that is a LIVE key. This dry run only READS live mode and "
            "creates nothing. The real run refuses a live key unless you pass "
            "--allow-live.",
            file=sys.stderr,
        )

    product_ids, problems = monthly_product_ids(key)
    if problems:
        if not args.allow_unrestricted:
            print(unresolved_restriction_message(problems), file=sys.stderr)
            return 1
        print(
            "--allow-unrestricted: creating coupons with NO product "
            "restriction, because " + ", ".join(problems) + ". This cannot be "
            "undone by re-running: Stripe will not add applies_to to a coupon "
            "that already exists.",
            file=sys.stderr,
        )

    if args.dry_run:
        print(f"DRY RUN: nothing is created. Key mode: {mode}", file=sys.stderr)
        print(
            f"Would create or verify {len(plan)} coupon(s), each applying to: "
            f"{restriction_label(product_ids)}",
            file=sys.stderr,
        )
        for entry in plan:
            print(
                f"  {entry['id']:<20} {entry['percent_off']:>3}%  "
                f"{entry['duration']:<8} {entry['name']}",
                file=sys.stderr,
            )
        _print_mode_reminder(mode)
        return 0

    print(f"Seeding referral coupons in {mode.upper()} mode.", file=sys.stderr)

    results: list[tuple[dict, str]] = []
    for entry in plan:
        _, outcome = ensure_coupon(entry, product_ids, key)
        results.append((entry, outcome))
        print(f"  {entry['id']:<20} {outcome}", file=sys.stderr)

    print(f"\n# ---- referral coupons ({mode} mode) ----")
    print(f"applies to: {restriction_label(product_ids)}")
    print(f"{'coupon id':<20} {'pct':>4}  {'duration':<8} result")
    for entry, outcome in results:
        print(
            f"{entry['id']:<20} {entry['percent_off']:>3}%  "
            f"{entry['duration']:<8} {outcome}"
        )
    created = sum(1 for _, outcome in results if outcome == "created")
    print(f"\n{created} created, {len(results) - created} already existed.")
    _print_mode_reminder(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
