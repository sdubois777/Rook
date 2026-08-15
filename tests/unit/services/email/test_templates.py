"""Tests for the email templates.

Two things are being defended here:

  1. Every percentage and price in the copy is DERIVED from REFERRAL_PROGRAM /
     TIER_LIMITS. The tests prove derivation rather than coincidence by moving
     the source value and asserting the copy moves with it — asserting "20%
     appears" would pass just as happily against a hardcoded 20.
  2. The referrer's reward email never carries anything identifying about the
     person who redeemed the code.
"""
from __future__ import annotations

import inspect

from backend.models.user import REFERRAL_PROGRAM, TIER_LIMITS
from backend.services.email import templates

APP_URL = "https://rookff.com"
UNSUB_URL = "https://rookff.com/api/email/unsubscribe?token=abc.def"
POSTAL = "Rook LLC, 1200 Example Ave, Austin, TX 78701"


def _welcome(**over):
    kwargs = {
        "display_name": "Stephen Dubois",
        "promo_code": "ROOK-WELCOME20",
        "referral_code": "ROOK-7K2M9X",
        "app_url": APP_URL,
        "unsubscribe_url": UNSUB_URL,
        "postal_address": POSTAL,
    }
    kwargs.update(over)
    return templates.welcome_email(**kwargs)


def _reward(**over):
    kwargs = {
        "display_name": "Stephen Dubois",
        "new_total_percent": 30,
        "referral_count": 3,
        "app_url": APP_URL,
        "unsubscribe_url": UNSUB_URL,
        "postal_address": POSTAL,
    }
    kwargs.update(over)
    return templates.referral_reward_email(**kwargs)


# ---------------------------------------------------------------------------
# welcome_email
# ---------------------------------------------------------------------------

class TestWelcomeEmail:
    def test_returns_subject_html_and_text(self):
        subject, html, text = _welcome()
        assert subject and html and text
        assert html.lstrip().startswith("<div")
        assert "<table" in html

    def test_welcome_percentage_comes_from_referral_program(self, monkeypatch):
        """Move the source of truth and the copy must move with it."""
        monkeypatch.setitem(REFERRAL_PROGRAM, "welcome_percent_off", 45)
        subject, html, text = _welcome()

        assert "45%" in subject
        assert "45%" in html
        assert "45%" in text
        assert "20%" not in html

    def test_referred_and_referrer_percentages_come_from_referral_program(
        self, monkeypatch
    ):
        monkeypatch.setitem(REFERRAL_PROGRAM, "referred_percent_off", 33)
        monkeypatch.setitem(REFERRAL_PROGRAM, "referrer_percent_off_per_referral", 7)
        monkeypatch.setitem(REFERRAL_PROGRAM, "referrer_percent_off_cap", 42)
        _, html, text = _welcome()

        for body in (html, text):
            assert "33%" in body
            assert "7%" in body
            assert "42%" in body

    def test_plan_price_comes_from_tier_limits(self, monkeypatch):
        monkeypatch.setitem(
            TIER_LIMITS["standard"], "price_monthly_usd", 11
        )
        _, html, text = _welcome()

        assert "$11" in html
        assert "$11" in text

    def test_contains_both_codes(self):
        _, html, text = _welcome()
        for body in (html, text):
            assert "ROOK-WELCOME20" in body
            assert "ROOK-7K2M9X" in body

    def test_explains_what_rook_is(self):
        _, html, text = _welcome()
        for body in (html, text):
            assert "valuations" in body
            assert "draft" in body

    def test_promotional_footer_carries_unsubscribe_and_postal_address(self):
        """CAN-SPAM: a commercial message needs both, in both alternatives."""
        _, html, text = _welcome()

        assert UNSUB_URL in html
        assert UNSUB_URL in text
        assert POSTAL in html
        assert POSTAL in text
        assert "unsubscribe" in html.lower()
        assert "unsubscribe" in text.lower()

    def test_greeting_uses_the_first_name_only(self):
        _, html, text = _welcome()
        assert "Hi Stephen," in text
        assert "Hi Stephen," in html
        assert "Stephen Dubois" not in text

    def test_missing_display_name_falls_back_to_a_neutral_greeting(self):
        _, html, text = _welcome(display_name=None)
        assert "Hi there," in text
        assert "Hi None" not in text

    def test_blank_display_name_falls_back_to_a_neutral_greeting(self):
        _, _, text = _welcome(display_name="   ")
        assert "Hi there," in text

    def test_display_name_is_html_escaped(self):
        """The name is user-controlled and reaches the message otherwise."""
        _, html, _ = _welcome(display_name="<script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_promo_code_is_html_escaped(self):
        _, html, _ = _welcome(promo_code="ROOK<img src=x>")
        assert "<img src=x>" not in html
        assert "&lt;img" in html

    def test_text_alternative_is_not_html(self):
        _, _, text = _welcome()
        assert "<table" not in text
        assert "style=" not in text
        assert "<p>" not in text

    def test_html_is_inline_styled_with_no_style_element(self):
        """Gmail strips <style>; a stylesheet would leave the message unstyled."""
        _, html, _ = _welcome()
        assert "<style" not in html.lower()
        assert "style=" in html

    def test_layout_is_capped_at_600px(self):
        _, html, _ = _welcome()
        assert "max-width:600px" in html


# ---------------------------------------------------------------------------
# referral_reward_email
# ---------------------------------------------------------------------------

class TestReferralRewardEmail:
    def test_states_the_new_total_and_the_count(self):
        subject, html, text = _reward(new_total_percent=30, referral_count=3)

        assert "30%" in subject
        for body in (html, text):
            assert "30%" in body
            assert "3 confirmed referrals" in body

    def test_one_referral_reads_as_singular(self):
        _, html, text = _reward(new_total_percent=10, referral_count=1)
        for body in (html, text):
            assert "1 confirmed referral" in body
            assert "1 confirmed referrals" not in body

    def test_below_the_cap_it_names_the_per_referral_step(self, monkeypatch):
        monkeypatch.setitem(REFERRAL_PROGRAM, "referrer_percent_off_per_referral", 7)
        monkeypatch.setitem(REFERRAL_PROGRAM, "referrer_percent_off_cap", 42)
        _, html, text = _reward(new_total_percent=14, referral_count=2)

        for body in (html, text):
            assert "7%" in body
            assert "42%" in body

    def test_at_the_cap_it_says_that_is_the_maximum(self, monkeypatch):
        monkeypatch.setitem(REFERRAL_PROGRAM, "referrer_percent_off_cap", 42)
        _, html, text = _reward(new_total_percent=42, referral_count=6)

        for body in (html, text):
            assert "maximum" in body.lower()

    def test_never_names_the_person_who_redeemed_the_code(self):
        """The referrer learns their own total and nothing about a third party.

        The signature check is the real assertion: there is no parameter that
        could carry the redeemer's identity, so no caller can leak one by
        mistake.
        """
        params = set(
            inspect.signature(templates.referral_reward_email).parameters
        )
        assert params == {
            "display_name",
            "new_total_percent",
            "referral_count",
            "app_url",
            "unsubscribe_url",
            "postal_address",
        }

        _, html, text = _reward()
        for leak in ("friend@example.com", "@gmail", "Jordan", "signed up as"):
            assert leak not in html
            assert leak not in text

    def test_promotional_footer_carries_unsubscribe_and_postal_address(self):
        _, html, text = _reward()

        assert UNSUB_URL in html
        assert UNSUB_URL in text
        assert POSTAL in html
        assert POSTAL in text

    def test_display_name_is_html_escaped(self):
        _, html, _ = _reward(display_name="<b>Sam</b>")
        assert "<b>Sam</b>" not in html
        assert "&lt;b&gt;Sam" in html

    def test_links_to_the_account_page(self):
        _, html, text = _reward()
        assert f"{APP_URL}/account" in html
        assert f"{APP_URL}/account" in text

    def test_text_alternative_is_not_html(self):
        _, _, text = _reward()
        assert "<table" not in text
        assert "style=" not in text
