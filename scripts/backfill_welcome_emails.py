"""
One-off: send the welcome email to accounts that signed up BEFORE it existed.

WHY THIS EXISTS. The welcome email fires from the Clerk `user.created` webhook, so
it only ever reaches accounts created after that code shipped. Everyone who signed
up earlier has a referral code they have never been told about and a first-month
discount code they have never been given. This walks those accounts once.

THIS SENDS REAL EMAIL TO REAL PEOPLE. It is the most dangerous script in the repo:
a mistake here is not a wrong number in a database, it is a message in somebody's
inbox that cannot be recalled, and a bad batch damages the sending domain's
reputation for every message after it. The safety posture is correspondingly
paranoid, and deliberately makes the safe path the one you get by forgetting:

  * It PRINTS A PLAN and sends nothing unless you pass --send.
  * Against a production database it additionally requires ROOK_ALLOW_PROD_WRITES=1
    (backend/db_guard.py), which is the same override every other prod-writing
    script in this repo takes.
  * --limit N caps the batch. Start at 1, read the message that arrives, then go.
  * Every send goes through EmailService, so it inherits the send lock: the dedupe
    key is `welcome:<user_id>`, exactly the key the webhook uses. Running this twice
    cannot send twice, and it cannot double up with the webhook for someone who
    signs up while it runs.
  * The suppression list is honoured, because EmailService checks it.

WHO IS SKIPPED, and why each rule exists:

  * ANYONE WHO HAS EVER HAD A SUBSCRIPTION. The welcome email carries a code for
    20% off a first month, and ReferralService REFUSES that code for an account
    that has ever subscribed (it is a new-customer offer). Mailing it to a current
    or former subscriber promises a discount that will be rejected at checkout,
    which is worse than sending nothing. Detected via users.subscription_status,
    which is NULL only for an account that has never had one.
  * SOFT-DELETED ACCOUNTS (users.deleted_at set).
  * SYNTHESIZED ADDRESSES — the @placeholder.local / @dev.local fallbacks the app
    invents when an identity provider gives us no address. Nothing resolves behind
    them, so a send is a guaranteed hard bounce. EmailService refuses them too;
    filtering here as well keeps them out of the plan and out of the counts.

RATE LIMITS, which are the thing most likely to bite on a real run:
  * Resend's free tier allows 100 emails per day. A batch larger than that will
    start failing partway through with provider errors.
  * EmailService also enforces settings.email_max_per_hour (default 200) per
    process, and a send stopped by that cap writes NO database row — the log line
    is its only record.
  Use --limit to stay under whichever ceiling applies to you. Failed sends are
  re-claimable, so a later run picks up what a rate limit stopped.

Run (PowerShell). Plan first, always:

    $env:ROOK_ENV_FILE = ".env.prod"
    uv run python scripts/backfill_welcome_emails.py --limit 1
    $env:ROOK_ALLOW_PROD_WRITES = "1"
    uv run python scripts/backfill_welcome_emails.py --limit 1 --send

GETTING A CODE TO A SUBSCRIBER, who is excluded from the batch on purpose:

    $env:ROOK_ALLOW_PROD_WRITES = "1"
    uv run python scripts/backfill_welcome_emails.py --code-for someone@example.com

That prints one account's referral code and share link and sends nothing. It is
still a write, because a code is minted on first read, so it takes the same prod
override. Put the code in a note you write yourself — a paying customer is the
best referrer you have, and a personal message from you converts better than a
template anyway.

No email address is printed in full unless --show-emails is passed; the plan masks
them by default so a shared terminal or a pasted log does not leak the list.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

# Run directly (`python scripts/backfill_welcome_emails.py`) and sys.path[0] is
# scripts/, not the repo root, so `backend` does not resolve. Same line every other
# script in this directory carries — see scripts/stripe_seed_referral_coupons.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings  # noqa: E402
from backend.database import AsyncSessionLocal  # noqa: E402
from backend.db_guard import db_host, guard_writes, is_prod_db  # noqa: E402
from backend.models.email import (  # noqa: E402
    SEND_SENT,
    UNDELIVERABLE_EMAIL_SUFFIXES,
)
from backend.models.user import User  # noqa: E402


# Reasons an account is left out of the batch. Kept as strings because they are
# printed as-is in the summary — an operator reading the output should not have to
# map a code to a meaning.
SKIP_SUBSCRIBED = "has (or had) a subscription — the welcome code would be refused"
SKIP_UNDELIVERABLE = "synthesized address, nothing resolves behind it"
SKIP_NO_EMAIL = "no email address on the account"


@dataclass
class Plan:
    eligible: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)   # reason -> count

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def mask(address: str) -> str:
    """`stephen@rookff.com` -> `s****n@rookff.com`. Enough to recognise an address
    you already know, not enough to harvest one you do not."""
    local, _, domain = (address or "").partition("@")
    if not domain:
        return "<no address>"
    if len(local) <= 2:
        return f"{local[:1]}*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def is_undeliverable(address: str) -> bool:
    a = (address or "").strip().lower()
    return not a or a.endswith(UNDELIVERABLE_EMAIL_SUFFIXES)


# Hosts that mean "this URL only works on the machine that generated it".
_LOCAL_URL_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", ".local")


def app_url_is_public(url: str) -> bool:
    """True when settings.app_url is an address a recipient could actually open.

    WHY THIS IS CHECKED, and why it is fatal rather than a warning. Every link in
    the email is built from settings.app_url: the button, the share link, and the
    unsubscribe link. This script points DATABASE_URL at production via
    ROOK_ENV_FILE=.env.prod, but that overlay carries only the database URL — every
    other setting still comes from the local .env, where APP_URL is a dev address.
    So it is entirely possible, and has happened, to mail production users a
    message whose every link points at localhost.

    The unsubscribe link is what makes this fatal instead of cosmetic: a
    promotional message whose opt-out does not work is a CAN-SPAM violation, not a
    broken button. Refusing to send is the only correct response.
    """
    u = (url or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    host = u.split("//", 1)[1].split("/", 1)[0]
    return not any(marker in host for marker in _LOCAL_URL_MARKERS)


async def build_plan(db, limit: int | None) -> Plan:
    """Read every live account and decide who is in the batch.

    Ordered oldest-first so a --limit run takes the earliest signups, which are the
    ones who have been waiting longest and are least likely to also be caught by
    the webhook mid-run.
    """
    rows = (
        await db.execute(
            select(User)
            .where(User.deleted_at.is_(None))
            .order_by(User.created_at.asc())
        )
    ).scalars().all()

    plan = Plan()
    for user in rows:
        if not (user.email or "").strip():
            plan.skip(SKIP_NO_EMAIL)
            continue
        if is_undeliverable(user.email):
            plan.skip(SKIP_UNDELIVERABLE)
            continue
        # NULL only for an account that never had a subscription. A cancelled
        # subscriber keeps a non-NULL value, which is what we want: the welcome
        # code is refused for them, so mailing it would promise nothing.
        if user.subscription_status is not None:
            plan.skip(SKIP_SUBSCRIBED)
            continue
        plan.eligible.append(user)

    if limit is not None:
        plan.eligible = plan.eligible[:limit]
    return plan


async def print_code_for(db, address: str) -> int:
    """Print one account's referral code, minting it if they have none. Sends nothing.

    WHY THIS IS HERE. A subscriber is deliberately excluded from the batch: the
    welcome email carries a first-month discount code that ReferralService refuses
    for anyone who has ever subscribed, so mailing it would promise a discount that
    fails at checkout. But a paying customer is the BEST referrer — they are paying
    and they stayed — so they still need their code. This gets it out of the
    database so it can go in a note written by a human.

    It is a WRITE, because a code is minted on first read and most accounts have
    never had theirs read. That is why it takes the prod override like --send does.
    """
    from backend.services.referral_service import ReferralService

    wanted = (address or "").strip().lower()
    user = (
        await db.execute(select(User).where(User.email == wanted))
    ).scalars().first()

    if user is None:
        print(f"\nNo account with the address {mask(wanted)}.")
        print("The lookup is exact and case-insensitive; check for a typo.")
        return 1
    if user.deleted_at is not None:
        print(f"\nThe account {mask(user.email)} is deleted.")
        return 1

    referrals = ReferralService.from_session(db)
    code = await referrals.get_or_create_code(user.id)
    await db.commit()

    print()
    print("=" * 72)
    print(f"  account       : {mask(user.email)}")
    print(f"  tier          : {user.tier}")
    print(f"  referral code : {code}")
    print(f"  share link    : {settings.app_url.rstrip('/')}/?ref={code}")
    print("=" * 72)
    if user.subscription_status is not None:
        print()
        print("  This account has (or had) a subscription, so it is NOT in the")
        print("  backfill batch — the welcome email's first-month discount code")
        print("  would be refused for them at checkout. The referral code above")
        print("  is still valid and is what they should share.")
    return 0


async def send_one(db, user) -> str:
    """Mint the codes for one account and send. Returns the SEND_* status.

    Both services are built on the SAME session the caller commits, because the
    referral code must be persisted before the email that quotes it goes out — an
    email naming a code that is not in the database would be worse than no email.
    """
    from backend.services.email.email_service import EmailService
    from backend.services.referral_service import ReferralService

    referrals = ReferralService.from_session(db)
    referral_code = await referrals.get_or_create_code(user.id)
    # get_or_create_code does not commit (callers do), and the send below commits
    # its own dedupe row — so commit the code first, deliberately, rather than
    # letting the email's commit sweep it along as a side effect.
    await db.commit()

    promo_code = referrals.welcome_code_for(user.id)
    return await EmailService.from_session(db).send_welcome(
        user=user, promo_code=promo_code, referral_code=referral_code
    )


def print_plan(plan: Plan, *, show_emails: bool) -> None:
    print()
    print("=" * 72)
    print("WELCOME EMAIL BACKFILL — PLAN")
    print("=" * 72)
    print(f"  database host : {db_host() or '<unknown>'}"
          f"{'   [PRODUCTION]' if is_prod_db() else ''}")
    print(f"  from address  : {settings.email_from}")
    print(f"  app url       : {settings.app_url}"
          f"{'' if app_url_is_public(settings.app_url) else '   [NOT PUBLIC]'}")
    print(f"  email enabled : {settings.email_enabled}")
    print(f"  promo allowed : {settings.promotional_email_enabled}")
    if not app_url_is_public(settings.app_url):
        print("     ^ EVERY LINK IN THE EMAIL is built from this, including the")
        print("       unsubscribe link. Pointing DATABASE_URL at production does")
        print("       NOT change APP_URL — .env.prod overlays only the database.")
        print("       Sending now would mail real users a dead opt-out link.")
        print("       Set APP_URL to the public site for this command.")
    if not settings.promotional_email_enabled:
        print("     ^ EMAIL_POSTAL_ADDRESS is empty, so every promotional send is")
        print("       refused. Set it before a real run or this does nothing.")
    print()
    print(f"  WOULD EMAIL   : {len(plan.eligible)}")
    for user in plan.eligible:
        shown = user.email if show_emails else mask(user.email)
        print(f"      {shown}")
    if plan.skipped:
        print()
        print("  SKIPPED:")
        for reason, count in sorted(plan.skipped.items()):
            print(f"      {count:5}  {reason}")
    print("=" * 72)


async def run(args) -> int:
    async with AsyncSessionLocal() as db:
        if args.code_for:
            return await print_code_for(db, args.code_for)

        plan = await build_plan(db, args.limit)
        print_plan(plan, show_emails=args.show_emails)

        if not args.send:
            print()
            print("PLAN ONLY — nothing was sent. Re-run with --send to send.")
            if is_prod_db():
                print("Against this production database --send ALSO requires")
                print("ROOK_ALLOW_PROD_WRITES=1.")
            return 0

        if not plan.eligible:
            print("\nNothing to send.")
            return 0

        if not settings.promotional_email_enabled:
            print("\nREFUSING TO SEND: promotional email is disabled because")
            print("EMAIL_POSTAL_ADDRESS is empty. US CAN-SPAM requires a physical")
            print("address on commercial email. Set it and re-run.")
            return 1

        if not app_url_is_public(settings.app_url):
            print(f"\nREFUSING TO SEND: APP_URL is {settings.app_url!r}, which no")
            print("recipient can open. Every link in the email is built from it,")
            print("including the unsubscribe link — and a promotional message whose")
            print("opt-out does not work is a CAN-SPAM violation.")
            print()
            print("Pointing the database at production does NOT fix this:")
            print("ROOK_ENV_FILE=.env.prod overlays only DATABASE_URL, so APP_URL")
            print("still comes from the local .env. Set it for this one command:")
            print('    $env:APP_URL = "https://rookff.com"')
            return 1

        print(f"\nSending to {len(plan.eligible)} account(s)...\n")
        counts: dict[str, int] = {}
        for user in plan.eligible:
            try:
                status = await send_one(db, user)
            except Exception as exc:  # one bad row must not end the batch
                status = f"error: {type(exc).__name__}"
                await db.rollback()
            counts[status] = counts.get(status, 0) + 1
            print(f"  {mask(user.email):40} {status}")

        print()
        print("=" * 72)
        for status, count in sorted(counts.items()):
            print(f"  {count:5}  {status}")
        print("=" * 72)
        sent = counts.get(SEND_SENT, 0)
        print(f"\n{sent} message(s) accepted by the provider.")
        if sent < len(plan.eligible):
            print("Anything that failed is re-claimable — a later run retries it,")
            print("up to the attempt ceiling. Anything skipped will stay skipped.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send the welcome email to accounts that predate it.",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="actually send. Without this the script only prints the plan.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="cap the batch (oldest signups first). Start with 1.",
    )
    parser.add_argument(
        "--show-emails", action="store_true",
        help="print full addresses instead of masked ones",
    )
    parser.add_argument(
        "--code-for", metavar="EMAIL", default=None,
        help=(
            "print ONE account's referral code (minting it if absent) and exit. "
            "Sends no email. Use this to get a code to a subscriber, who is "
            "excluded from the batch on purpose."
        ),
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.code_for and args.send:
        parser.error("--code-for prints one code and sends nothing; drop --send")

    # Prod write guard on both mutating paths. Building a PLAN is exempt: it only
    # reads, and requiring an override just to look is how people stop looking.
    # --code-for is NOT exempt — it mints a referral code, which is a write.
    if args.send:
        guard_writes("send welcome emails to existing accounts")
    elif args.code_for:
        guard_writes("mint a referral code for one account")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
