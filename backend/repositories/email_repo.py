"""
Outbound email repository — the suppression list and the send lock.

Same conflict-tolerant posture as backend/repositories/billing_repo.py: every
write here can arrive twice (a redelivered webhook, a second unsubscribe click,
two app processes), so each one states in SQL what a second arrival should do.
`suppress` is ON CONFLICT DO NOTHING and reports whether the row was new.
`claim_send` is ON CONFLICT DO UPDATE under a WHERE clause, because a second
arrival there is sometimes a duplicate to refuse and sometimes a retry of a
failed send to allow — see its docstring for the full state table.

ADDRESS NORMALIZATION. Every method lowercases and strips the address on the way
in. Email local parts are case-sensitive in the RFC but no real provider treats
them that way, and the suppression table's primary key is the raw string — so
storing "Sam@X.com" and later checking "sam@x.com" would miss, and we would mail
someone who unsubscribed. Normalizing in one place is what stops that.

COMMITS. This repository exposes commit() and its caller uses it, which is the
opposite of the house rule that repositories never commit. The reason is that a
send is an irreversible side effect: the dedupe_key row has to be durable BEFORE
the provider is called, or a crash between the insert and the send rolls back the
lock and the retry mails the person twice. See EmailService.send for the ordering.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.email import (
    EmailSend,
    EmailSuppression,
    MAX_SEND_ATTEMPTS,
    SEND_FAILED,
    SEND_PENDING,
)

# A provider error body can be an entire HTML page. The column is TEXT so it would
# store the lot, but a 200KB error string in a log table helps nobody and makes the
# row painful to read in psql.
_ERROR_MAX_CHARS = 2000


def normalize_email(email: Optional[str]) -> str:
    """Lowercased, stripped address. Empty string for None."""
    return (email or "").strip().lower()


class EmailRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def is_suppressed(self, email: str) -> bool:
        """True when this address must never be sent promotional mail again."""
        result = await self._session.execute(
            select(EmailSuppression.email).where(
                EmailSuppression.email == normalize_email(email)
            )
        )
        return result.scalar_one_or_none() is not None

    async def suppress(self, email: str, reason: str) -> bool:
        """Add an address to the suppression list; True if it was newly added.

        False means it was already suppressed — a second unsubscribe click, or a
        bounce for an address that had already complained. Either way the desired
        state already holds, so the caller treats False as success, not an error.
        """
        result = await self._session.execute(
            pg_insert(EmailSuppression)
            .values(email=normalize_email(email), reason=reason)
            .on_conflict_do_nothing(index_elements=["email"])
        )
        return result.rowcount > 0

    async def claim_send(
        self,
        *,
        dedupe_key: str,
        to_email: str,
        template: str,
        category: str,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[EmailSend]:
        """Claim the right to make one provider call for this key. THE SEND LOCK.

        Returns the claimed row, or None when the claim was refused — in which
        case the caller must NOT call the provider.

        WHICH STATES ARE CLAIMABLE. Three statuses can appear in this table —
        'pending' (written here), and 'sent' or 'failed' (written by
        mark_status). Nothing else is ever stored:

          no row yet                              claimable — inserted, attempts 1
          status 'failed', attempts < MAX_SEND_ATTEMPTS
                                                  claimable — re-taken, attempts + 1
          status 'failed', attempts >= MAX_SEND_ATTEMPTS
                                                  refused, the retry ceiling
          status 'sent'                           refused at ANY attempt count
          status 'pending'                        refused — a provider call is in
                                                  flight, or one crashed mid-call
                                                  and nobody knows whether the
                                                  message went out

        SKIPPED AND SUPPRESSED SENDS LEAVE NO ROW AT ALL. They are statuses
        EmailService.send RETURNS, never statuses it stores: every check that
        produces one (email disabled, promotional mail disabled, undeliverable
        address, suppressed recipient, hourly cap) returns before claim_send is
        reached. An operator investigating a message that never arrived and
        finding no row here has learned something specific — the send was
        refused upstream of the lock, and the application log is the only record
        of it.

        The row is never deleted. Deleting it to allow a retry would reopen the
        double-send race the UNIQUE key exists to close, so retry is expressed as
        re-claiming the same row under a WHERE clause that only 'failed' can
        satisfy. Before this, a provider outage burned the dedupe key
        permanently: the row survived with status 'failed' and every later
        attempt was refused forever, so the message was never delivered and
        nothing ever retried it.

        `attempts` is incremented HERE rather than after the provider responds,
        because the claim is taken only in order to make the call and
        EmailService makes it unconditionally once the claim commits. Counting
        afterwards would lose the increment exactly when it matters most — a
        process that dies mid-call — and let a crash loop retry without bound.

        The row lands with status SEND_PENDING, not SEND_SKIPPED: 'pending' means
        the provider call was started and the result is unknown, which is a state
        an operator must investigate, while 'skipped' means we chose not to send.
        Collapsing the two makes every unexplained row look like a choice.
        mark_status overwrites it with the real outcome.

        Two statements rather than `.returning(EmailSend)`: RETURNING an ORM
        entity together with ON CONFLICT has enough version-dependent behaviour
        in SQLAlchemy that a plain column RETURNING plus a get() is the cheaper
        thing to be sure about. The get() runs only on the winning path.
        """
        insert_stmt = pg_insert(EmailSend).values(
            id=uuid.uuid4(),
            user_id=user_id,
            to_email=normalize_email(to_email),
            template=template,
            category=category,
            dedupe_key=dedupe_key,
            status=SEND_PENDING,
            attempts=1,
        )
        result = await self._session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["dedupe_key"],
                # Unqualified EmailSend columns on the right-hand side read the
                # EXISTING row, so this is "one more than whatever is stored".
                # `excluded` is the row this INSERT proposed, so to_email takes
                # the address of THIS attempt.
                set_={
                    "status": SEND_PENDING,
                    "attempts": EmailSend.attempts + 1,
                    # The address can legitimately differ from the first
                    # attempt's — the identity provider's email may have changed,
                    # or the first attempt ran while a placeholder address was in
                    # play. Without this the row would name the old address while
                    # the message went to the new one, which is an audit row that
                    # lies about where mail was sent. template, category and
                    # user_id are NOT refreshed: the dedupe key identifies one
                    # message to one user, so the address is the only one of them
                    # that can change.
                    "to_email": insert_stmt.excluded.to_email,
                    # The previous failure's message would otherwise sit on the
                    # row describing an attempt that is no longer the last one.
                    "error": None,
                    "provider_message_id": None,
                },
                # The whole retry rule, enforced by the database rather than by a
                # read-then-write that two workers could interleave. A conflict
                # that fails this WHERE updates nothing and RETURNING yields no
                # row, which is the refusal.
                where=and_(
                    EmailSend.status == SEND_FAILED,
                    EmailSend.attempts < MAX_SEND_ATTEMPTS,
                ),
            ).returning(EmailSend.id)
        )
        send_id = result.scalar_one_or_none()
        if send_id is None:
            return None
        return await self._session.get(EmailSend, send_id)

    async def mark_status(
        self,
        send_id: uuid.UUID,
        status: str,
        *,
        provider_message_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record the outcome of the provider call on an already-claimed row."""
        await self._session.execute(
            update(EmailSend)
            .where(EmailSend.id == send_id)
            .values(
                status=status,
                provider_message_id=provider_message_id,
                error=(error[:_ERROR_MAX_CHARS] if error else None),
            )
        )

    async def commit(self) -> None:
        await self._session.commit()
