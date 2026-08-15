"""
ReferralService — code generation and the single validation entry point.

Two discount codes exist and they behave differently:

  welcome   A per-user string emailed to a free signup who has not paid. It is
            derived from the user id with an HMAC under settings.secret_key, so
            it only works for the account it was issued to. It cannot be shared,
            posted to a coupon site, or guessed from another user's code.
  referral  A per-user random string. Redeeming one pays its owner a recurring
            reward, so it carries every anti-farming rule.

`resolve_code` is the ONLY place a code is judged. The checkout endpoint, the
validate-code endpoint, and anything added later all call it, so a rule added
here applies everywhere at once. Every percentage comes from REFERRAL_PROGRAM in
backend/models/user.py — none are written down in this file.

RESERVATIONS. Resolving a code is not enough on its own: two checkouts opened at
the same time both resolve, both get a Stripe session carrying the coupon, and
both can be paid. `reserve_for_checkout` takes the redeemer's one-per-kind slot
BEFORE the discounted session exists, so the second attempt loses at the database.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional

from backend.config import settings
from backend.models.referral import KIND_REFERRAL, KIND_WELCOME, STATUS_PENDING
from backend.models.user import (
    REFERRAL_PROGRAM,
    effective_tier,
    interval_is_referral_eligible,
    referrer_percent_off,
)

logger = logging.getLogger(__name__)

# One generic rejection for every "this code does not apply to you" case. Saying
# "that code belongs to another user" or "no such code" would let anyone probe
# the code space and learn which strings are live codes.
_GENERIC_REJECTION = "That code is not valid."

# Shown when the account's slot is held by a checkout that is still payable, as
# opposed to a discount that has already been spent. Public so the checkout
# endpoint can raise the SAME sentence when its own reservation loses the race —
# the two are one situation reached by two routes, and a user who reads different
# explanations for it will think one of them is a bug.
CHECKOUT_IN_FLIGHT_MESSAGE = (
    "A checkout using this discount is already open. "
    "Finish it, or try again in a few minutes."
)


def _has_ever_been_billed(user) -> bool:
    """True when this account has completed a paid plan at some point.

    THE SIGNAL IS users.subscription_status. It is written only by the verified
    Stripe webhook, which sets it on every successful checkout (monthly and
    season) and never sets it back to None — _on_subscription_deleted leaves it
    at "active" while dropping the tier to free. So a non-NULL value means money
    was taken for this account at least once, which is what the welcome code's
    audience rule actually means. effective_tier() alone answers only "is this
    account paid RIGHT NOW", and a subscriber who cancelled reads as free again.

    ONE CASE STILL SLIPS THROUGH, knowingly. GET /account/me writes
    subscription_status back to None as part of the lazy write-back when a SEASON
    pass expires (backend/routers/account.py). A lapsed season purchaser who then
    loads the account page can take the welcome discount on a later monthly
    checkout. Closing it means not clearing that field, which is the account
    router's decision, not this module's.
    """
    return getattr(user, "subscription_status", None) is not None

# How many fresh codes to draw before giving up. A collision needs two users to
# draw the same string out of 30^6 (roughly 729 million), so three attempts is
# already far past the point of caring; the bound exists so a broken alphabet or
# an exhausted keyspace fails loudly instead of spinning.
_MAX_CODE_ATTEMPTS = 3

# ── welcome code construction ───────────────────────────────────────────
#
# The welcome code is derived, not stored: HMAC-SHA256 over the user id under
# settings.secret_key, mapped into the same alphabet the shareable codes use.
#
# The purpose tag is part of the signed body so this signature can never be
# confused with the unsubscribe-token signature, which is computed with the same
# key over a different body (backend/services/email/unsubscribe.py). Bump the
# version in the tag to invalidate every outstanding welcome code at once.
_WELCOME_PURPOSE = "welcome-code:v1:"

# The letter that separates a welcome code from a shareable one, and how many
# characters follow it.
#
# LENGTH IS THE SAFETY PROPERTY. generate_code draws exactly code_length (6)
# characters after the prefix; a welcome body is 1 + 10 = 11 characters. No draw
# can ever produce a welcome code, and no welcome code can ever collide with
# somebody's shareable code, whatever the alphabet contains. The infix "W" is
# only there to make the two visually distinct in a support conversation.
#
# 10 characters out of a 31-character alphabet is about 2^49 possibilities, and a
# guess only pays out for the ONE account it was derived for, so there is nothing
# to spray at.
_WELCOME_INFIX = "W"
_WELCOME_BODY_LENGTH = 10


@dataclass(frozen=True)
class ResolvedCode:
    """The verdict on one code, for one user, for one billing interval."""

    valid: bool
    kind: Optional[str]
    percent_off: int
    referrer_user_id: Optional[uuid.UUID]
    message: str


def _rejected(message: str) -> ResolvedCode:
    return ResolvedCode(
        valid=False,
        kind=None,
        percent_off=0,
        referrer_user_id=None,
        message=message,
    )


class ReferralService:
    def __init__(self, repo, user_repo):
        self._repo = repo
        # Read for two things: that a code's owner is still a live account
        # before promising a discount that depends on paying them a reward, and
        # that a welcome code's redeemer is neither paying now nor has paid
        # before (tier plus subscription_status — see _has_ever_been_billed).
        self._users = user_repo

    @classmethod
    def from_session(cls, db) -> "ReferralService":
        from backend.repositories.referral_repo import ReferralRepository
        from backend.repositories.user_repo import UserRepository

        return cls(ReferralRepository(db), UserRepository(db))

    # ── codes ───────────────────────────────────────────────────────────

    def generate_code(self) -> str:
        """Draw a fresh shareable code, e.g. "ROOK-7K2M9X".

        `secrets` rather than `random`: a code is worth money to whoever holds it,
        and `random` is seeded predictably enough that a stream of codes issued
        close together can be reproduced.
        """
        alphabet = REFERRAL_PROGRAM["code_alphabet"]
        body = "".join(
            secrets.choice(alphabet)
            for _ in range(REFERRAL_PROGRAM["code_length"])
        )
        return f"{REFERRAL_PROGRAM['code_prefix']}-{body}"

    def welcome_code_for(self, user_id: uuid.UUID) -> str:
        """This user's own welcome code, e.g. "ROOK-W7K2M9XQRTV".

        Derived, never stored, so there is no table to read and no code to leak.
        Two properties matter:

          * It is different for every account, so learning one code buys nothing.
            Validation recomputes the code for the REDEEMING user, which means a
            code posted publicly is worthless to anyone who did not receive it.
          * It cannot be produced by generate_code (see _WELCOME_BODY_LENGTH), so
            a shareable code can never be mistaken for a welcome code.

        Each digest byte is folded into the alphabet with a modulo. The alphabet
        is 31 characters and a byte is 256 values, so the first eight characters
        of the alphabet are drawn very slightly more often than the rest. That
        costs a fraction of a bit per character out of ~49 and is not worth a
        rejection-sampling loop here.
        """
        alphabet = REFERRAL_PROGRAM["code_alphabet"]
        digest = hmac.new(
            settings.secret_key.encode(),
            f"{_WELCOME_PURPOSE}{user_id}".encode(),
            hashlib.sha256,
        ).digest()
        body = "".join(
            alphabet[byte % len(alphabet)]
            for byte in digest[:_WELCOME_BODY_LENGTH]
        )
        return f"{REFERRAL_PROGRAM['code_prefix']}-{_WELCOME_INFIX}{body}"

    @staticmethod
    def _has_welcome_shape(normalized: str) -> bool:
        """True when a string could be somebody's welcome code.

        This is the routing test AND the input guard. hmac.compare_digest raises
        TypeError on a non-ASCII str, so nothing reaches it until every character
        has been checked against the prefix and the code alphabet — both of which
        are ASCII.
        """
        head = f"{REFERRAL_PROGRAM['code_prefix']}-{_WELCOME_INFIX}"
        if len(normalized) != len(head) + _WELCOME_BODY_LENGTH:
            return False
        if not normalized.startswith(head):
            return False
        return set(normalized[len(head):]) <= set(REFERRAL_PROGRAM["code_alphabet"])

    async def get_or_create_code(self, user_id: uuid.UUID) -> str:
        """This user's shareable code, minting one on first call.

        DOES NOT COMMIT. A caller that mints a code must commit its own session,
        or the new row is discarded when the session closes. The commit used to
        live here and was removed because this can run inside the Stripe webhook's
        transaction, where an early commit would persist half-applied entitlement
        state that a later failure could no longer roll back.

        Idempotent under concurrency. Two simultaneous requests both find no row
        and both insert; the UNIQUE(user_id) constraint lets exactly one win and
        the loser's insert is skipped rather than raised (see
        ReferralRepository.create_code), so it re-reads and returns the winner's
        code. Both callers get the same string.
        """
        existing = await self._repo.get_code_for_user(user_id)
        if existing is not None:
            return existing.code

        for _ in range(_MAX_CODE_ATTEMPTS):
            created = await self._repo.create_code(user_id, self.generate_code())
            if created is not None:
                return created
            # The insert was skipped: either another request just gave this user a
            # code, or the drawn code is already someone else's. Re-reading tells
            # us which — a row means the former, so return it; no row means the
            # latter, so draw again.
            existing = await self._repo.get_code_for_user(user_id)
            if existing is not None:
                return existing.code

        raise RuntimeError(
            f"Could not allocate a referral code after {_MAX_CODE_ATTEMPTS} attempts"
        )

    # ── validation ──────────────────────────────────────────────────────

    async def resolve_code(
        self,
        *,
        code: str,
        redeemer_user_id: uuid.UUID,
        interval: str,
    ) -> ResolvedCode:
        """Judge a code for one user and one billing interval.

        Returns a verdict rather than raising, because both callers need the
        message: checkout turns an invalid verdict into a 400, and the
        validate-code endpoint shows it next to the input box.
        """
        normalized = (code or "").strip().upper()
        if not normalized:
            return _rejected(_GENERIC_REJECTION)

        # Interval first, so a season buyer is told the real reason instead of
        # being told their perfectly good code is invalid.
        if not interval_is_referral_eligible(interval):
            return _rejected("Referral discounts apply to monthly plans only.")

        if self._has_welcome_shape(normalized):
            return await self._resolve_welcome(normalized, redeemer_user_id)
        return await self._resolve_referral(normalized, redeemer_user_id)

    async def _resolve_welcome(
        self, normalized: str, redeemer_user_id: uuid.UUID
    ) -> ResolvedCode:
        # Recompute the code for the account that is redeeming it. Somebody
        # else's welcome code fails here, which is what stops the string from
        # being shared, forwarded, or posted on a coupon site.
        if not hmac.compare_digest(normalized, self.welcome_code_for(redeemer_user_id)):
            return _rejected(_GENERIC_REJECTION)

        # The welcome discount exists to convert a signup who has NEVER PAID.
        # Two separate reasons to refuse it, both needed:
        #
        #   effective_tier != free   they are paying right now. Catches a paid
        #                            tier granted outside Stripe as well.
        #   ever billed              they paid before. A subscriber who cancelled
        #                            reads as free again, so the tier test alone
        #                            would hand them the new-customer discount on
        #                            their way back in — which is the opposite of
        #                            what the rejection sentence promises.
        redeemer = await self._users.get(redeemer_user_id)
        if redeemer is None:
            return _rejected(_GENERIC_REJECTION)
        if effective_tier(redeemer) != "free" or _has_ever_been_billed(redeemer):
            return _rejected(
                "Welcome codes are for accounts that have not subscribed yet."
            )

        blocking = await self._repo.blocking_redemption_status(
            redeemer_user_id, KIND_WELCOME
        )
        if blocking == STATUS_PENDING:
            return _rejected(CHECKOUT_IN_FLIGHT_MESSAGE)
        if blocking is not None:
            return _rejected("You have already used a welcome code.")

        percent_off = REFERRAL_PROGRAM["welcome_percent_off"]
        return ResolvedCode(
            valid=True,
            kind=KIND_WELCOME,
            percent_off=percent_off,
            # Nobody earns a reward for a welcome code — there is no referrer.
            referrer_user_id=None,
            message=f"{percent_off}% off your first month.",
        )

    async def _resolve_referral(
        self, normalized: str, redeemer_user_id: uuid.UUID
    ) -> ResolvedCode:
        row = await self._repo.get_by_code(normalized)
        if row is None:
            return _rejected(_GENERIC_REJECTION)

        # Self-referral. Named plainly rather than given the generic message: the
        # user already knows this code is theirs, so there is nothing to leak, and
        # "that code is not valid" for your own code reads like a bug.
        if row.user_id == redeemer_user_id:
            return _rejected("You cannot use your own referral code.")

        # A code whose owner no longer has an account cannot pay out. Treated as
        # unknown, using the same generic message, so the redeemer learns nothing
        # about the state of somebody else's account.
        owner = await self._users.get(row.user_id)
        if owner is None or getattr(owner, "deleted_at", None) is not None:
            return _rejected(_GENERIC_REJECTION)

        # Mutual referral. Two people who were both going to subscribe anyway can
        # otherwise redeem each other's codes and each earn a permanent recurring
        # reward, having acquired nobody. The message stays generic: confirming
        # "that account already used your code" would tell the redeemer that the
        # other account exists and what it did.
        if await self._repo.has_redeemed_from(
            redeemer_user_id=row.user_id, referrer_user_id=redeemer_user_id
        ):
            return _rejected(_GENERIC_REJECTION)

        # Same split as the welcome path: a checkout that is still open is a
        # different situation from a discount that has already been spent, and
        # this is the only place either message is produced. Without the split
        # the "already used" sentence fires first and a user with a genuinely
        # in-flight checkout is told they spent a discount they have not spent.
        blocking = await self._repo.blocking_redemption_status(
            redeemer_user_id, KIND_REFERRAL
        )
        if blocking == STATUS_PENDING:
            return _rejected(CHECKOUT_IN_FLIGHT_MESSAGE)
        if blocking is not None:
            return _rejected("You have already used a referral code.")

        percent_off = REFERRAL_PROGRAM["referred_percent_off"]
        return ResolvedCode(
            valid=True,
            kind=KIND_REFERRAL,
            percent_off=percent_off,
            referrer_user_id=row.user_id,
            message=f"{percent_off}% off your first month.",
        )

    # ── checkout reservations ───────────────────────────────────────────
    #
    # These three COMMIT, which every other method here deliberately does not.
    # The reason is the same one that makes the email send-lock commit: a
    # reservation nobody else can see is not a reservation. A concurrent request
    # runs in its own database session, so until this transaction commits the
    # second insert simply blocks on the unique index — and it would be blocking
    # across our Stripe network call. They are only ever reached from an HTTP
    # request, never from the webhook, so there is no entitlement state pending on
    # the session when they run.

    async def reserve_for_checkout(
        self, *, resolved: ResolvedCode, redeemer_user_id: uuid.UUID, code: str
    ) -> Optional[uuid.UUID]:
        """Claim this user's one-per-kind slot; return the row id, or None if lost.

        None means a checkout with this kind of discount is already in flight or
        already complete for this account. The caller must NOT create a discounted
        Stripe session in that case.

        The row is written with a provisional session id because the real Stripe
        session does not exist yet — the whole point is to take the slot first.
        `attach_checkout_session` swaps in the real id once Stripe answers.
        """
        reservation_id = await self._repo.reserve_redemption(
            kind=resolved.kind,
            code=code,
            redeemer_user_id=redeemer_user_id,
            referrer_user_id=resolved.referrer_user_id,
            # Unique and obviously not a Stripe id, so a half-finished checkout is
            # recognisable in the table without joining anything.
            stripe_session_id=f"reserved_{uuid.uuid4()}",
            percent_off=resolved.percent_off,
        )
        if reservation_id is not None:
            await self._repo.commit()
        return reservation_id

    async def attach_checkout_session(
        self, reservation_id: uuid.UUID, stripe_session_id: str
    ) -> None:
        """Point a reservation at the Stripe session that was created for it.

        The webhook finds the row by this id, so a reservation that never gets one
        can never be confirmed — it just expires and stops blocking.
        """
        await self._repo.attach_session_id(reservation_id, stripe_session_id)
        await self._repo.commit()

    async def release_reservation(self, reservation_id: uuid.UUID) -> None:
        """Drop a reservation whose Stripe session was never created.

        Without this, a Stripe outage would hold the user's once-ever slot for the
        full pending TTL over a checkout that never existed.
        """
        await self._repo.release_reservation(reservation_id)
        await self._repo.commit()

    # ── referrer view ───────────────────────────────────────────────────

    async def referrer_state(self, user_id: uuid.UUID) -> dict:
        """Everything the account page shows about this user's own code.

        percent_off is the CURRENT total rate, derived by referrer_percent_off
        from the confirmed count — it is never accumulated or stored, so a
        referral that stops paying lowers it on the next read with no correction
        step.

        DOES NOT COMMIT, and it can MINT a code (get_or_create_code). A caller
        that never commits will show the user a code that was rolled back, and
        the next read will show them a different one.
        """
        code = await self.get_or_create_code(user_id)
        count = await self._repo.confirmed_referral_count(user_id)
        return {
            "code": code,
            "referral_count": count,
            "percent_off": referrer_percent_off(count),
            "percent_off_cap": REFERRAL_PROGRAM["referrer_percent_off_cap"],
            "percent_off_per_referral": REFERRAL_PROGRAM[
                "referrer_percent_off_per_referral"
            ],
        }
