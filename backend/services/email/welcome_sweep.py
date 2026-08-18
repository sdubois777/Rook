"""Backstop that mails any account the primary welcome trigger missed.

WHY THIS EXISTS. The welcome email had exactly ONE trigger: the Clerk user.created
webhook in backend/routers/webhooks.py. That trigger never fired in production —
no webhook endpoint was ever created in Clerk — so for months every account was
created by the first-authenticated-request path (UserService.get_or_create, reached
from backend/core/dependencies.py), which sends nothing and has no hook to. Not one
welcome email was sent automatically. The only ones customers received came from
somebody running scripts/backfill_welcome_emails.py by hand.

Nothing detected it, because a skipped send writes no row to email_sends and the
process discarded INFO-level logs. A missing welcome email and a delivered one were
indistinguishable from inside the system.

So the fix is not only "configure the webhook". A single trigger with no backstop
fails permanently and silently the first time anything goes wrong with it. This
sweep is the backstop, and it is deliberately indifferent to WHY the primary
trigger did not fire: a missing endpoint, a rotated signing secret, a provider
outage, a deploy landing mid-signup. It asks one question — which accounts have no
welcome email — and answers it.

SAFETY. This is a job that emails real customers, so it is bounded in three
independent ways:
  * A batch ceiling per run (welcome_sweep_batch), so a defect here mails a handful
    of people rather than the entire users table.
  * A minimum account age (welcome_sweep_min_age_minutes), so the webhook remains
    the normal sender and the sweep only collects what it missed.
  * The send lock in EmailService: the dedupe key welcome:<user_id> is claimed
    before the provider call, so an account already mailed cannot be mailed twice
    no matter how often this runs, and a race with the webhook is harmless.
The per-process hourly cap in email_service.py sits underneath all three.

Placeholder addresses (@dev.local and similar) are excluded, because mailing them
earns hard bounces and a bounce rate is what gets a sending domain blocked.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models.email import UNDELIVERABLE_EMAIL_SUFFIXES, EmailSend
from backend.models.user import User
from backend.utils.seasons import asof_now

logger = logging.getLogger(__name__)

TEMPLATE_WELCOME = "welcome"


# The prefix EmailService.send_welcome builds its dedupe key from: the first send
# claims "welcome:<user_id>", and a corrected resend claims
# "welcome:<user_id>:<reason>". Both mean the account has been welcomed.
WELCOME_KEY_PREFIX = "welcome:"


def _welcome_key(user_id) -> str:
    """The dedupe key for a first send to this account.

    FOR A REAL uuid ONLY. Do NOT pass a SQLAlchemy column to this: an f-string on
    a column interpolates the column's repr, so the pattern becomes the literal
    text "welcome:users.id", matches no row, and every account then looks
    unwelcomed. That defect was caught by running the sweep read-only against
    production, where it selected 16 customers who had already been mailed. Build
    the SQL pattern with _welcome_key_sql() instead.
    """
    return f"{WELCOME_KEY_PREFIX}{user_id}"


def _welcome_key_sql():
    """SQL expression for "any welcome dedupe key belonging to this user row".

    Concatenation happens in the DATABASE, per row, which is the whole point —
    see the warning on _welcome_key above.
    """
    return literal(WELCOME_KEY_PREFIX) + cast(User.id, String) + literal("%")


def _already_welcomed():
    """Correlated EXISTS: this user row has at least one welcome send."""
    return (
        select(EmailSend.id)
        .where(EmailSend.dedupe_key.like(_welcome_key_sql()))
        .correlate(User)
        .exists()
    )


async def find_accounts_missing_welcome(
    db: AsyncSession, *, limit: int, min_age_minutes: int,
) -> list[User]:
    """Accounts old enough to have been mailed, that have no welcome send at all.

    Matches on dedupe_key rather than on (user_id, template) because a corrected
    resend writes welcome:<id>:<reason> against the same user, and a user who
    received only a corrected copy has still been welcomed. Any row whose key
    starts with the account's welcome prefix counts.

    Oldest first, so a backlog drains in signup order rather than the newest
    account repeatedly winning the batch.
    """
    cutoff = asof_now() - timedelta(minutes=min_age_minutes)

    stmt = (
        select(User)
        .where(
            User.created_at <= cutoff,
            User.deleted_at.is_(None),
            User.email.isnot(None),
            User.email != "",
            ~_already_welcomed(),
        )
        .order_by(User.created_at.asc())
        .limit(limit)
    )
    users = list((await db.execute(stmt)).scalars().all())

    # Placeholder addresses are filtered in Python rather than SQL so the one
    # list of synthesized domains (backend/models/email.py) stays the only list.
    return [
        u for u in users
        if not (u.email or "").strip().lower().endswith(UNDELIVERABLE_EMAIL_SUFFIXES)
    ]


async def count_accounts_missing_welcome(db: AsyncSession) -> int:
    """How many accounts have no welcome email, ignoring age and batch limits.

    This is the invariant nothing asserted before: a signup should produce a
    welcome send. Exposed so it can be read from a status endpoint instead of
    being discovered when a customer says they never got one.
    """
    stmt = select(func.count()).select_from(User).where(
        User.deleted_at.is_(None),
        User.email.isnot(None),
        User.email != "",
        ~_already_welcomed(),
    )
    return int((await db.execute(stmt)).scalar_one())


async def run_welcome_sweep(db: AsyncSession) -> dict:
    """Mail every account in this batch that has no welcome email.

    Never raises: this runs on the scheduler, and one bad account must not stop
    the others or kill the job. Returns a summary for the caller to log.
    """
    from backend.services.email.email_service import SEND_SENT, EmailService
    from backend.services.referral_service import ReferralService

    if not settings.welcome_sweep_enabled:
        return {"enabled": False, "found": 0, "sent": 0, "failed": 0}

    users = await find_accounts_missing_welcome(
        db,
        limit=settings.welcome_sweep_batch,
        min_age_minutes=settings.welcome_sweep_min_age_minutes,
    )
    if not users:
        return {"enabled": True, "found": 0, "sent": 0, "failed": 0, "outcomes": {}}

    logger.warning(
        "Welcome sweep: %d account(s) have no welcome email. The primary trigger "
        "(the Clerk user.created webhook) did not deliver for these — check that "
        "the webhook endpoint exists in Clerk and that its signing secret matches "
        "CLERK_WEBHOOK_SECRET.",
        len(users),
    )

    referrals = ReferralService.from_session(db)
    emails = EmailService.from_session(db)
    outcomes: dict[str, int] = {}
    sent = failed = 0

    for user in users:
        try:
            # Mint and COMMIT the referral code before sending: the message
            # contains it, so a code rolled back after delivery would leave the
            # recipient holding a code that does not exist.
            referral_code = await referrals.get_or_create_code(user.id)
            await db.commit()

            status = await emails.send_welcome(
                user=user,
                promo_code=referrals.welcome_code_for(user.id),
                referral_code=referral_code,
            )
        except Exception:
            logger.exception(
                "Welcome sweep: could not mail account %s", user.id
            )
            failed += 1
            continue

        outcomes[status] = outcomes.get(status, 0) + 1
        if status == SEND_SENT:
            sent += 1
        else:
            failed += 1
            # Loud, because this is the backstop. If the backstop is also not
            # sending, nothing else is going to catch it.
            logger.error(
                "Welcome sweep: account %s returned %s instead of a send. No "
                "email_sends row is written for most non-send outcomes, so this "
                "line is the record.",
                user.id, status,
            )

    logger.warning(
        "Welcome sweep finished: %d found, %d sent, %d not sent (%s)",
        len(users), sent, failed, outcomes or "no outcomes",
    )
    return {
        "enabled": True,
        "found": len(users),
        "sent": sent,
        "failed": failed,
        "outcomes": outcomes,
    }
