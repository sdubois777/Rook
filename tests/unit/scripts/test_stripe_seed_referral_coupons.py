"""Tests for the referral-coupon seeder.

The seeder is pointed at a real Stripe account by hand, so the failures worth
testing are the ones that are silent when they happen:

  * a coupon id that does not derive from REFERRAL_PROGRAM — the runtime asks
    Stripe for a coupon nobody created and the discount just never applies;
  * a --dry-run that is not actually dry;
  * a re-run that tries to recreate a coupon instead of leaving it alone;
  * a coupon created without its applies_to restriction, which discounts the
    season products and the credit packs and can never be repaired, because
    Stripe will not add applies_to to a coupon that already exists.

The stripe SDK is replaced wholesale by `_FakeStripe`, which records calls and
makes no network request, so nothing here can reach Stripe.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "stripe_seed_referral_coupons.py"
_spec = importlib.util.spec_from_file_location(
    "stripe_seed_referral_coupons_under_test", _SCRIPT
)
seeder = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = seeder
_spec.loader.exec_module(seeder)

from backend.models.user import (  # noqa: E402
    REFERRAL_PROGRAM,
    referral_percent_tiers,
)


class _InvalidRequestError(Exception):
    """Stands in for stripe.InvalidRequestError, which carries a `code`."""

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        self.code = code


class _FakeStripe:
    """Every stripe attribute the seeder touches, and nothing else.

    `calls` is the assertion surface: a dry run must leave it empty, and a
    re-run must contain retrieves but no creates.
    """

    def __init__(self, existing: tuple[str, ...] = (), prices: dict | None = None):
        self.InvalidRequestError = _InvalidRequestError
        self.calls: list[tuple] = []
        self.created: list[dict] = []
        self._existing = set(existing)
        self._prices = prices or {}
        self.Coupon = SimpleNamespace(
            retrieve=self._coupon_retrieve, create=self._coupon_create
        )
        self.Price = SimpleNamespace(retrieve=self._price_retrieve)

    def _coupon_retrieve(self, cid, api_key=None):
        self.calls.append(("Coupon.retrieve", cid))
        if cid in self._existing:
            return {"id": cid}
        raise _InvalidRequestError(f"No such coupon: {cid}", code="resource_missing")

    def _coupon_create(self, **kwargs):
        self.calls.append(("Coupon.create", kwargs["id"]))
        self.created.append(kwargs)
        self._existing.add(kwargs["id"])
        return {"id": kwargs["id"]}

    def _price_retrieve(self, price_id, api_key=None):
        self.calls.append(("Price.retrieve", price_id))
        return {"product": self._prices[price_id]}


@pytest.fixture
def stripe_env(monkeypatch):
    """A test key in the env, both monthly prices configured, fake SDK installed."""
    fake = _FakeStripe(prices={"price_std_monthly": "prod_std",
                               "price_pro_monthly": "prod_pro"})
    monkeypatch.setattr(seeder, "stripe", fake)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_seeder_unit_test")
    monkeypatch.setattr(
        seeder.settings, "stripe_price_standard_monthly", "price_std_monthly"
    )
    monkeypatch.setattr(
        seeder.settings, "stripe_price_pro_monthly", "price_pro_monthly"
    )
    return fake


# ---------------------------------------------------------------------------
# The coupon set derives from REFERRAL_PROGRAM
# ---------------------------------------------------------------------------

def test_coupon_ids_match_the_referral_percent_tiers():
    """One forever coupon per rate the referrer reward can reach, plus the two
    one-time coupons — no more, no fewer."""
    ids = {entry["id"] for entry in seeder.coupon_plan()}
    expected = {f"rook_forever_{pct}" for pct in referral_percent_tiers()}
    expected |= {
        f"rook_once_{REFERRAL_PROGRAM['welcome_percent_off']}",
        f"rook_once_{REFERRAL_PROGRAM['referred_percent_off']}",
    }
    assert ids == expected


def test_referrer_coupons_are_forever_and_the_others_are_once():
    by_id = {entry["id"]: entry for entry in seeder.coupon_plan()}
    for pct in referral_percent_tiers():
        assert by_id[f"rook_forever_{pct}"]["duration"] == "forever"
        assert by_id[f"rook_forever_{pct}"]["percent_off"] == pct
    welcome = by_id[f"rook_once_{REFERRAL_PROGRAM['welcome_percent_off']}"]
    assert welcome["duration"] == "once"


def test_percentages_are_read_from_the_program_not_hardcoded(monkeypatch):
    """Change the program, and the plan changes with it.

    This is the real test for 'no percentage is written literally in the
    script' — a source scan cannot tell a hardcoded rate from a column width.
    """
    monkeypatch.setattr(seeder, "REFERRAL_PROGRAM", {
        "welcome_percent_off": 15,
        "referred_percent_off": 45,
    })
    monkeypatch.setattr(seeder, "referral_percent_tiers", lambda: (5, 10))
    ids = {entry["id"] for entry in seeder.coupon_plan()}
    assert ids == {"rook_once_15", "rook_once_45",
                   "rook_forever_5", "rook_forever_10"}


def test_two_roles_on_the_same_rate_collapse_to_one_coupon(monkeypatch):
    """welcome and referred are separate settings and may be set to the same
    percentage. Two entries with one id would make the second create fail."""
    monkeypatch.setattr(seeder, "REFERRAL_PROGRAM", {
        "welcome_percent_off": 25,
        "referred_percent_off": 25,
    })
    monkeypatch.setattr(seeder, "referral_percent_tiers", lambda: (10,))
    plan = seeder.coupon_plan()
    once = [e for e in plan if e["duration"] == "once"]
    assert len(once) == 1
    assert once[0]["metadata"]["rook_roles"] == "welcome/referred"


def test_coupon_id_scheme_is_the_one_the_runtime_uses():
    assert seeder.coupon_id("once", 20) == "rook_once_20"
    assert seeder.coupon_id("forever", 30) == "rook_forever_30"


def test_catalog_asks_stripe_for_ids_this_script_creates():
    """Drift guard against the runtime counterpart.

    backend/services/billing/catalog.py builds the coupon id when a discount is
    attached at checkout or on a subscription. If its scheme and this one ever
    diverge, the runtime asks Stripe for a coupon that was never created: the
    customer pays full price and nothing errors on our side.
    """
    from backend.models.referral import KIND_REFERRAL, KIND_WELCOME
    from backend.services.billing import catalog

    seeded = {entry["id"] for entry in seeder.coupon_plan()}
    assert catalog.referral_coupon_id(KIND_WELCOME) in seeded
    assert catalog.referral_coupon_id(KIND_REFERRAL) in seeded
    for pct in referral_percent_tiers():
        assert catalog.referrer_coupon_id(pct) in seeded


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_creates_nothing(stripe_env, capsys):
    """It reads prices to name the products; it must touch no coupon at all."""
    assert seeder.main(["--dry-run"]) == 0
    assert stripe_env.created == []
    assert [c for c in stripe_env.calls if c[0].startswith("Coupon")] == []


def test_dry_run_prints_every_coupon_it_would_create(stripe_env, capsys):
    seeder.main(["--dry-run"])
    out = capsys.readouterr()
    printed = out.out + out.err
    for entry in seeder.coupon_plan():
        assert entry["id"] in printed


def test_dry_run_names_the_products_the_coupons_would_be_restricted_to(
    stripe_env, capsys
):
    """The operator has one chance to notice a missing restriction — before the
    coupons exist. So the dry run prints the product ids, not a count."""
    assert seeder.main(["--dry-run"]) == 0
    printed = capsys.readouterr()
    combined = printed.out + printed.err
    assert "prod_std" in combined
    assert "prod_pro" in combined


def test_dry_run_refuses_without_a_key(monkeypatch, capsys):
    """A dry run resolves the restriction against Stripe, so with no key it
    cannot answer the question it was asked. Its exit code matches the real
    run's."""
    fake = _FakeStripe()
    monkeypatch.setattr(seeder, "stripe", fake)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setattr(seeder.settings, "stripe_secret_key", None)
    assert seeder.main(["--dry-run"]) == 1
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_existing_coupon_is_skipped_not_recreated(stripe_env, capsys):
    for entry in seeder.coupon_plan():
        stripe_env._existing.add(entry["id"])

    assert seeder.main([]) == 0
    assert stripe_env.created == []
    assert not [c for c in stripe_env.calls if c[0] == "Coupon.create"]
    summary = capsys.readouterr().out
    assert "exists" in summary
    assert "0 created" in summary


def test_only_the_missing_coupons_are_created(stripe_env, capsys):
    already = seeder.coupon_plan()[0]["id"]
    stripe_env._existing.add(already)

    assert seeder.main([]) == 0
    created_ids = [kwargs["id"] for kwargs in stripe_env.created]
    assert already not in created_ids
    assert len(created_ids) == len(seeder.coupon_plan()) - 1


def test_a_second_run_creates_nothing(stripe_env):
    assert seeder.main([]) == 0
    first = len(stripe_env.created)
    assert first == len(seeder.coupon_plan())
    assert seeder.main([]) == 0
    assert len(stripe_env.created) == first


def test_a_retrieve_failure_that_is_not_resource_missing_is_not_swallowed(stripe_env):
    """Creating over an error we did not understand would be a blind write."""
    def boom(cid, api_key=None):
        raise _InvalidRequestError("bad id", code="parameter_invalid")

    stripe_env.Coupon.retrieve = boom
    with pytest.raises(_InvalidRequestError):
        seeder.main([])


# ---------------------------------------------------------------------------
# Monthly-only restriction
# ---------------------------------------------------------------------------

def test_coupons_are_restricted_to_the_monthly_products(stripe_env):
    seeder.main([])
    for kwargs in stripe_env.created:
        assert kwargs["applies_to"] == {"products": ["prod_std", "prod_pro"]}


def test_missing_price_id_creates_nothing_and_exits_non_zero(
    stripe_env, monkeypatch, capsys
):
    """The live-money case. Railway injects env vars and reads no .env file, so
    an unset price id is None in production. Creating unrestricted coupons there
    is permanent — Stripe will not add applies_to afterwards — so the run stops."""
    monkeypatch.setattr(seeder.settings, "stripe_price_pro_monthly", None)

    assert seeder.main([]) == 1
    assert stripe_env.created == []
    assert not [c for c in stripe_env.calls if c[0] == "Coupon.create"]
    printed = capsys.readouterr().err
    assert "REFUSING" in printed
    assert "STRIPE_PRICE_PRO_MONTHLY" in printed
    assert "NOTHING WAS CREATED" in printed
    assert "--allow-unrestricted" in printed


def test_price_from_another_mode_creates_nothing_and_exits_non_zero(
    stripe_env, capsys
):
    """Test-mode price ids do not resolve under a live key. That is a stop, not
    a warning: the coupons it would create are the unrestricted ones."""
    def missing(price_id, api_key=None):
        raise _InvalidRequestError("No such price", code="resource_missing")

    stripe_env.Price.retrieve = missing

    assert seeder.main([]) == 1
    assert stripe_env.created == []
    assert "no such price in this mode" in capsys.readouterr().err


def test_dry_run_refuses_the_same_way_the_real_run_does(
    stripe_env, monkeypatch, capsys
):
    """A dry run that reported success and a real run that then refused would be
    worse than no dry run."""
    monkeypatch.setattr(seeder.settings, "stripe_price_standard_monthly", None)

    assert seeder.main(["--dry-run"]) == 1
    assert "REFUSING" in capsys.readouterr().err


def test_allow_unrestricted_is_the_only_way_to_create_an_unrestricted_coupon(
    stripe_env, monkeypatch, capsys
):
    monkeypatch.setattr(seeder.settings, "stripe_price_pro_monthly", None)

    assert seeder.main(["--allow-unrestricted"]) == 0
    assert stripe_env.created, "nothing was created"
    # A partial restriction would lock the unconfigured tier out of the discount
    # entirely, so one missing id drops the restriction for every coupon.
    for kwargs in stripe_env.created:
        assert "applies_to" not in kwargs
    assert "--allow-unrestricted" in capsys.readouterr().err


def test_allow_unrestricted_still_restricts_when_the_products_resolve(stripe_env):
    """The flag permits the unsafe fallback; it does not ask for it. With both
    price ids readable there is no reason to drop a restriction that costs
    nothing, and a stray flag must not create permanently unrestricted coupons."""
    assert seeder.main(["--allow-unrestricted"]) == 0
    assert stripe_env.created, "nothing was created"
    for kwargs in stripe_env.created:
        assert kwargs["applies_to"] == {"products": ["prod_std", "prod_pro"]}


# ---------------------------------------------------------------------------
# Safety posture
# ---------------------------------------------------------------------------

def test_live_key_is_refused_without_allow_live(stripe_env, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_seeder_unit_test")
    assert seeder.main([]) == 1
    assert stripe_env.calls == []


def test_live_key_runs_with_allow_live(stripe_env, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_seeder_unit_test")
    assert seeder.main(["--allow-live"]) == 0
    assert stripe_env.created


def test_unrecognisable_key_is_refused(stripe_env, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "not-a-stripe-key")
    assert seeder.main([]) == 1
    assert stripe_env.calls == []


def test_missing_key_is_an_error(stripe_env, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setattr(seeder.settings, "stripe_secret_key", None)
    assert seeder.main([]) == 1
    assert stripe_env.calls == []


def test_the_secret_key_is_never_printed(stripe_env, monkeypatch, capsys):
    key = "sk_test_super_secret_value"
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    seeder.main([])
    printed = capsys.readouterr()
    assert key not in printed.out + printed.err
    assert "super_secret_value" not in printed.out + printed.err


def test_the_summary_reminds_the_operator_that_coupons_are_per_mode(stripe_env, capsys):
    seeder.main([])
    out = capsys.readouterr().out
    assert "per MODE" in out
    assert "live" in out
