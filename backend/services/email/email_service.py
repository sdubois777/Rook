"""
EmailService — the one path every outbound message takes.

THE CONTRACT: NOTHING PUBLIC HERE RAISES. That covers send() and equally
send_welcome() and send_referral_reward(), which are what callers actually reach
for. Every caller is a webhook handler (Clerk user.created, Stripe
checkout.session.completed) whose job is to record an entitlement. An email that
fails to send must not fail the webhook, because the provider then retries the
whole event and the entitlement work runs again. So every failure here is
caught, recorded on the email_sends row where there is one, and returned as a
status string.

The wrappers need their own guard rather than inheriting send()'s: they mint the
unsubscribe token and render the template BEFORE calling send(), so a renamed
dict key in templates.py or a non-str display_name would raise past send()
entirely and into the webhook.

ORDER OF CHECKS. Cheap and local first, provider call last:

  1. email disabled (no RESEND_API_KEY)                     -> skipped
  2. promotional while promotional mail is disabled         -> skipped
  3. address is empty, malformed, or a synthesized
     placeholder domain                                     -> skipped
  4. address is on the suppression list                     -> suppressed
  5. process hourly cap reached                             -> skipped
  6. dedupe_key is not claimable                            -> skipped
  7. provider call                                          -> sent | failed

Check 3 exists because backend/core/dependencies.py and backend/routers/draft.py
both synthesize an address when the identity provider gives us none. The domains
they use are listed once, in UNDELIVERABLE_EMAIL_SUFFIXES in
backend/models/email.py, and imported here.

Check 5 is the only check that records NOTHING. It fires before the dedupe claim,
so a capped send leaves no email_sends row at all and its log line is the only
trace of it.

Check 6 is not "has this key been used before". A key whose last attempt FAILED
is claimable again while `attempts` stays under MAX_SEND_ATTEMPTS, which counts
provider calls in total rather than retries — see EmailRepository.claim_send for
the full state table. Without that, a single provider outage burned the key
permanently and the message was never delivered by anything.

RE-CLAIMING A FAILURE IS ONLY SAFE BECAUSE OF THE IDEMPOTENCY KEY. A provider
call can fail two ways, and one of them is a message that may already have been
accepted (a timeout on the 10-second budget). Marking that 'failed' makes it
re-claimable, and a second attempt would be a duplicate email — which is the one
thing the dedupe row exists to prevent. So send() passes `dedupe_key` on to
resend_gateway.send_email, which sends it as the provider's Idempotency-Key: a
repeat of the same request returns the original result instead of mailing the
person again. The residual (Resend's key window is 24 hours) is documented in
resend_gateway.py. The failure log line names which of the two cases happened,
because a rejection and a timeout call for different operator responses.

TRANSACTIONS. send() commits the claim before calling the provider and commits
the outcome after. That is deliberate and it is why EmailRepository exposes
commit() at all: an email cannot be un-sent, so the dedupe row must survive a
crash or a caller rollback that happens after the message is already gone. The
consequence for callers is real — send() commits whatever else is pending on the
session it was built from. Call it after your own commit, or build it from a
separate session.

RETRY IS CALLER-DRIVEN. Nothing in this module reschedules a failed send; there
is no retry job. A failed row simply stops refusing the next attempt that comes
through the same code path — a redelivered webhook, or an operator re-running the
call. A message whose only trigger fires once still needs a job to be written
before it is genuinely retried.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from threading import Lock
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models.email import (
    CATEGORY_PROMOTIONAL,
    SEND_FAILED,
    SEND_SENT,
    SEND_SKIPPED,
    SEND_SUPPRESSED,
    UNDELIVERABLE_EMAIL_SUFFIXES,
)
from backend.repositories.email_repo import EmailRepository, normalize_email
from backend.services.email import resend_gateway, templates
from backend.services.email.unsubscribe import (
    make_token,
    unsubscribe_url,
    unsubscribe_url_for_token,
)

logger = logging.getLogger(__name__)

TEMPLATE_WELCOME = "welcome"
TEMPLATE_REFERRAL_REWARD = "referral_reward"

_HOUR_SECONDS = 3600


class _HourlySendCap:
    """Process-local sliding-hour counter on provider calls.

    PER-PROCESS, like backend/middleware/rate_limit.py, and for the same reason:
    there is one app process today. With N processes the effective ceiling is
    N x the configured value. That fails in the permissive direction, which is
    accepted here because the cap is a blast-radius limit on a runaway loop —
    the failure it exists to contain is a loop over the whole users table, which
    would blow through any multiple of 200 immediately and still be caught.
    """

    def __init__(self) -> None:
        self._sends: deque[float] = deque()
        self._lock = Lock()

    def try_consume(self, limit: int) -> bool:
        """Take a slot if one is free. False means the cap is reached.

        Consuming here, before the dedupe claim, can burn a slot on a send that
        then turns out to be a duplicate. That errs toward sending less, which is
        the safe direction for the thing this cap protects (the sending domain's
        reputation).
        """
        now = time.time()
        with self._lock:
            cutoff = now - _HOUR_SECONDS
            while self._sends and self._sends[0] <= cutoff:
                self._sends.popleft()
            if len(self._sends) >= limit:
                return False
            self._sends.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._sends.clear()


# Module-level so the cap spans every request and background task in the process.
_hourly_cap = _HourlySendCap()


def _is_deliverable(address: str) -> bool:
    """False for an empty, malformed, or synthesized placeholder address.

    Not a validator — the provider does that, and an over-strict regex here would
    refuse legitimate addresses. This rejects only the shapes that cannot be
    delivered under any reading: no local part ("@x.com"), no dot in the domain
    ("a@b", which is a bare hostname and not a routable public domain), a domain
    with a leading or trailing dot, and the synthesized placeholder domains.

    Requiring the dot matters because a "@" alone was the previous bar, and the
    strings that reach this function come from an identity provider or from an
    app-side fallback rather than from a validated form field.
    """
    if not address:
        return False
    # rpartition: the LAST "@" separates local part from domain, so a quoted
    # local part containing "@" does not split in the wrong place.
    local, at, domain = address.rpartition("@")
    if not at or not local or not domain:
        return False
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    return not address.endswith(UNDELIVERABLE_EMAIL_SUFFIXES)


# The three phrases the provider-failure log line can carry. They are spelled out
# rather than derived from the exception class name so the log says what the
# operator has to decide, not what our type hierarchy is called.
_OUTCOME_REJECTED = (
    "REJECTED (Resend answered with an error status — the message was not "
    "accepted and nothing is queued at the provider)"
)
_OUTCOME_UNKNOWN = (
    "UNKNOWN (timeout or transport failure — Resend may already have accepted "
    "the message; there is no way to tell from here)"
)
_OUTCOME_NOT_ATTEMPTED = (
    "FAILED BEFORE THE PROVIDER ANSWERED (misconfiguration or a bug on our "
    "side — treat as not sent)"
)


def _provider_outcome(exc: BaseException) -> str:
    """Which of the three provider-failure cases this exception is.

    The distinction matters to whoever reads the log: a rejection is a message
    that definitely did not go out, while a timeout is a message that may
    already be in the recipient's inbox. Both leave a re-claimable row, which is
    only safe because every request carries an idempotency key.
    """
    if isinstance(exc, resend_gateway.ResendRejected):
        return _OUTCOME_REJECTED
    if isinstance(exc, resend_gateway.ResendAmbiguous):
        return _OUTCOME_UNKNOWN
    return _OUTCOME_NOT_ATTEMPTED


class EmailService:
    def __init__(self, repo: EmailRepository):
        self._repo = repo

    @classmethod
    def from_session(cls, db: AsyncSession) -> "EmailService":
        return cls(EmailRepository(db))

    async def send(
        self,
        *,
        to_email: str,
        subject: str,
        html: str,
        text: str,
        template: str,
        category: str,
        dedupe_key: str,
        user_id: Optional[uuid.UUID] = None,
        unsubscribe_token: Optional[str] = None,
    ) -> str:
        """Send one message. Returns one of the SEND_* status constants.

        Never raises. See the module docstring for the order of checks.
        """
        address = normalize_email(to_email)

        if not settings.email_enabled:
            logger.info(
                "Email disabled (no RESEND_API_KEY) — skipping %s", template
            )
            return SEND_SKIPPED

        if category == CATEGORY_PROMOTIONAL and not settings.promotional_email_enabled:
            # Almost always a missing EMAIL_POSTAL_ADDRESS. Sending a commercial
            # message without a physical address is a CAN-SPAM violation, so this
            # refuses rather than degrading the footer.
            logger.warning(
                "Promotional email disabled (EMAIL_POSTAL_ADDRESS unset) — "
                "skipping %s",
                template,
            )
            return SEND_SKIPPED

        if not _is_deliverable(address):
            logger.info(
                "Skipping %s — address is not deliverable (placeholder or "
                "malformed)",
                template,
            )
            return SEND_SKIPPED

        # From here on, anything can touch the database, and none of it is
        # allowed to escape to the webhook that called us.
        try:
            if await self._repo.is_suppressed(address):
                logger.info("Skipping %s — recipient is suppressed", template)
                return SEND_SUPPRESSED

            if not _hourly_cap.try_consume(settings.email_max_per_hour):
                # The cap is consumed before the claim, so NOTHING is written for
                # a capped send — this log line is the only record that it
                # happened, which is why it names the recipient and the template.
                # Do not send an operator to email_sends for these; the row is
                # not there.
                logger.error(
                    "Hourly email cap of %s reached — dropped %s to %s. Capped "
                    "sends are NOT recorded in email_sends; this log line is "
                    "the only record. Cause is either a traffic spike or a send "
                    "loop.",
                    settings.email_max_per_hour,
                    template,
                    address,
                )
                return SEND_SKIPPED

            # Built BEFORE the claim commits. Everything between the commit and
            # the provider call runs with the dedupe key already burned, so a
            # raise in there would consume the only chance to send this message
            # without ever calling the provider. Nothing but the provider call
            # belongs after that commit.
            headers = self._headers(category, address, unsubscribe_token)

            row = await self._repo.claim_send(
                dedupe_key=dedupe_key,
                to_email=address,
                template=template,
                category=category,
                user_id=user_id,
            )
            if row is None:
                logger.info(
                    "Skipping %s — dedupe key %s is already sent, in flight, or "
                    "past the retry ceiling",
                    template,
                    dedupe_key,
                )
                return SEND_SKIPPED

            # Read the id BEFORE the commit. backend/database.py sets
            # expire_on_commit=False globally, so reading row.id afterwards
            # happens to work — but under SQLAlchemy's default the commit would
            # expire the instance and the attribute access would trigger a lazy
            # refresh, which raises MissingGreenlet under asyncio. That raise
            # would land AFTER the message was already delivered. Holding the id
            # in a local removes the dependency on that global setting.
            send_id = row.id

            # Durable BEFORE the provider call — that is what makes the claim a
            # lock rather than a note.
            await self._repo.commit()
        except Exception:
            logger.exception("Email pre-send checks failed for %s", template)
            return SEND_FAILED

        try:
            message_id = await resend_gateway.send_email(
                to=address,
                subject=subject,
                html=html,
                text=text,
                # The send lock's key is also the provider's idempotency key, so
                # a re-claimed retry of an ambiguous failure returns the original
                # result instead of delivering a second copy. See the module
                # docstring in resend_gateway.py for the 24-hour residual.
                dedupe_key=dedupe_key,
                headers=headers or None,
            )
        except Exception as exc:
            logger.error(
                "Provider call for %s ended %s: %s. A later attempt may re-claim "
                "this row; the idempotency key on the request is what keeps that "
                "from delivering a second copy.",
                template,
                _provider_outcome(exc),
                exc,
            )
            # SEND_FAILED, not SEND_PENDING: this is the state claim_send will
            # let a later attempt re-take, while attempts stay under
            # MAX_SEND_ATTEMPTS.
            await self._record(send_id, SEND_FAILED, error=str(exc))
            return SEND_FAILED

        await self._record(send_id, SEND_SENT, provider_message_id=message_id)
        return SEND_SENT

    @staticmethod
    def _headers(
        category: str, address: str, unsubscribe_token: Optional[str]
    ) -> dict[str, str]:
        """One-click unsubscribe headers on promotional mail.

        Gmail and Yahoo require List-Unsubscribe plus List-Unsubscribe-Post on
        bulk mail; without them a sender gets throttled or filtered regardless of
        what the message body offers.
        """
        if category != CATEGORY_PROMOTIONAL:
            return {}
        link = (
            unsubscribe_url_for_token(unsubscribe_token)
            if unsubscribe_token
            else unsubscribe_url(address)
        )
        return {
            "List-Unsubscribe": f"<{link}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

    async def _record(
        self,
        send_id: uuid.UUID,
        status: str,
        *,
        provider_message_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Write the outcome. A failure here is logged, never raised — the
        message has already left, and losing the bookkeeping must not turn into
        a failed webhook."""
        try:
            await self._repo.mark_status(
                send_id,
                status,
                provider_message_id=provider_message_id,
                error=error,
            )
            await self._repo.commit()
        except Exception:
            logger.exception(
                "Could not record email send outcome %s for %s", status, send_id
            )

    # -----------------------------------------------------------------------
    # Template convenience methods
    #
    # These are what the Clerk and Stripe webhook handlers call, so THEY are the
    # methods that carry the never-raises contract in practice. Each one mints a
    # token and renders a template before send() is reached, and neither of those
    # steps is inside send()'s guard: templates.py reads keys out of
    # REFERRAL_PROGRAM and TIER_LIMITS (a rename raises KeyError) and formats
    # caller-supplied values (a non-str display_name raises inside .strip()).
    # Unguarded, either would raise into a webhook, the provider would treat the
    # event as undelivered, and it would be redelivered indefinitely. So both
    # bodies sit inside try/except and return a status string like send() does.
    # -----------------------------------------------------------------------

    async def send_welcome(
        self,
        *,
        user,
        promo_code: str,
        referral_code: str,
        dedupe_suffix: str | None = None,
    ) -> str:
        """Welcome + first-month discount code for a new free signup.

        Never raises. Returns a SEND_* status; SEND_FAILED if the message could
        not even be built.

        `dedupe_suffix` DELIBERATELY LETS THE SAME USER BE MAILED AGAIN, and exists
        for one narrow case: a batch went out wrong and has to be corrected. It
        happened once already — a run against the production database used a
        development APP_URL, so twelve recipients got a message whose every link,
        including the unsubscribe link, pointed at localhost.

        The alternative was deleting those rows so the normal key could be reused.
        That is worse: it erases the evidence that a broken message was sent, which
        is the record you most want during the conversation that follows. A suffix
        writes a NEW row and leaves the original intact.

        Pass a short reason, e.g. "fixed-links". Never pass a timestamp or a random
        value — the key would stop deduplicating and a retry would mail twice.
        """
        try:
            token = make_token(user.email)
            subject, html, text = templates.welcome_email(
                display_name=getattr(user, "display_name", None),
                promo_code=promo_code,
                referral_code=referral_code,
                app_url=settings.app_url.rstrip("/"),
                unsubscribe_url=unsubscribe_url_for_token(token),
                postal_address=settings.email_postal_address,
            )
            return await self.send(
                to_email=user.email,
                subject=subject,
                html=html,
                text=text,
                template=TEMPLATE_WELCOME,
                category=CATEGORY_PROMOTIONAL,
                dedupe_key=(
                    f"welcome:{user.id}:{dedupe_suffix}"
                    if dedupe_suffix
                    else f"welcome:{user.id}"
                ),
                user_id=user.id,
                unsubscribe_token=token,
            )
        except Exception:
            # No email_sends row exists at this point — the failure is upstream
            # of the claim — so this log line is the only record.
            logger.exception(
                "Could not build the welcome email for user %s",
                getattr(user, "id", None),
            )
            return SEND_FAILED

    async def send_referral_reward(
        self, *, user, new_total_percent: int, referral_count: int
    ) -> str:
        """Tell a referrer their discount went up.

        The dedupe key carries the count, so referral number 2 and referral
        number 3 are separate messages while a redelivered webhook for either one
        is not.

        Never raises. Returns a SEND_* status; SEND_FAILED if the message could
        not even be built.
        """
        try:
            token = make_token(user.email)
            subject, html, text = templates.referral_reward_email(
                display_name=getattr(user, "display_name", None),
                new_total_percent=new_total_percent,
                referral_count=referral_count,
                app_url=settings.app_url.rstrip("/"),
                unsubscribe_url=unsubscribe_url_for_token(token),
                postal_address=settings.email_postal_address,
            )
            return await self.send(
                to_email=user.email,
                subject=subject,
                html=html,
                text=text,
                template=TEMPLATE_REFERRAL_REWARD,
                category=CATEGORY_PROMOTIONAL,
                dedupe_key=f"referral_reward:{user.id}:{referral_count}",
                user_id=user.id,
                unsubscribe_token=token,
            )
        except Exception:
            logger.exception(
                "Could not build the referral reward email for user %s",
                getattr(user, "id", None),
            )
            return SEND_FAILED
