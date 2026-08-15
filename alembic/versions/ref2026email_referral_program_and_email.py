"""referral program tables and outbound email tables

Revision ID: ref2026email
Revises: wvr2026settings
Create Date: 2026-08-15

WHY. Two features land together because the referral program is delivered BY
email: a signup who never pays is emailed a discount code, and a code only
spreads if its owner can see it and send it.

Four new tables, no changes to any existing table. That is deliberate —
railway.toml starts the service with `alembic upgrade head && uvicorn`, so a
migration that rewrites a populated table is downtime. CREATE TABLE takes no lock
on anything already in use.

referral_codes
  One shareable code per user. UNIQUE on user_id as well as on code, so a
  concurrent double-generate loses at the database instead of leaving one user
  holding two live codes that both attribute rewards.

code_redemptions
  One row per discount actually applied at checkout, covering both kinds
  (a friend's referral code, and the welcome code emailed to a free signup).

  Two constraints carry the anti-farming rules, and both are here rather than in
  application code because both must hold under concurrent webhook delivery:

    UNIQUE(stripe_session_id)          Stripe delivers at-least-once and a
                                       redelivery can carry a different event
                                       id, so the checkout session id is the
                                       only stable key. Same idempotency shape
                                       as granted_pack_sessions.

    UNIQUE(redeemer_user_id, kind)     Each account takes each discount kind at
                                       most once, ever. This intentionally still
                                       binds after a reversal: a refunded
                                       redemption keeps its row with
                                       status='reversed' and keeps occupying the
                                       slot, so subscribe -> discount -> refund
                                       -> repeat does not work.

email_sends
  One row per dedupe_key, carrying the outcome of the most recent attempt against
  it. The key is UNIQUE and is claimed BEFORE the provider is called, which is
  what makes the send lock work.

  The row is never deleted, because deleting it reopens the double-send race it
  exists to close. Retry is therefore expressed as RE-CLAIMING the same row: a
  claim can be re-taken only while the status is 'failed' and `attempts` is below
  the ceiling, and a row that reached 'sent' is never re-claimable at any attempt
  count. So a retried webhook against a failed row DOES reach the provider, and
  against a sent row does not.

  Note what does NOT get a row: a send skipped because email is disabled, because
  the address is undeliverable, or because the hourly cap tripped returns before
  the claim is attempted and writes nothing at all. An operator looking for a
  missing message will not find those here — only in the application log.

email_suppressions
  Addresses that must never be emailed again (unsubscribe, hard bounce, spam
  complaint). Keyed on the lowercased address rather than user_id: a bounce or
  complaint arrives from the provider identified only by address, and an address
  that complained must stay suppressed even if the account is deleted and
  recreated. The address IS the primary key, so a double unsubscribe click is a
  no-op rather than a duplicate row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'ref2026email'
down_revision: Union[str, None] = 'wvr2026settings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'referral_codes',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('code', sa.String(24), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_referral_codes_user'),
        sa.UniqueConstraint('code', name='uq_referral_codes_code'),
    )
    op.create_index('ix_referral_codes_user_id', 'referral_codes', ['user_id'])
    op.create_index('ix_referral_codes_code', 'referral_codes', ['code'])

    op.create_table(
        'code_redemptions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('code', sa.String(24), nullable=False),
        sa.Column('redeemer_user_id', postgresql.UUID(as_uuid=True), nullable=False),
        # NULL for welcome-code redemptions — nobody earns a reward for those.
        sa.Column('referrer_user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('stripe_session_id', sa.String(255), nullable=False),
        sa.Column('percent_off', sa.Integer(), nullable=False),
        # server_default as well as the model default: this column is NOT NULL and
        # a raw-SQL insert path that omits it would otherwise fail at runtime.
        #
        # Three values, and the order matters:
        #   pending    written when the Checkout Session is CREATED, before the
        #              customer pays. This is a reservation: it makes the
        #              (redeemer_user_id, kind) unique constraint fire on a second
        #              concurrent checkout, so a user cannot open two tabs and get
        #              the same discount applied to two subscriptions. A pending row
        #              older than the Stripe session lifetime is treated as
        #              abandoned and does not block a fresh attempt.
        #   confirmed  the webhook saw the payment complete.
        #   reversed   the referred subscription went away; the referrer's earned
        #              rate drops accordingly.
        sa.Column(
            'status',
            sa.String(16),
            nullable=False,
            server_default='pending',
        ),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'stripe_session_id', name='uq_redemption_stripe_session'
        ),
        sa.UniqueConstraint(
            'redeemer_user_id', 'kind', name='uq_redemption_user_kind'
        ),
    )
    op.create_index('ix_code_redemptions_code', 'code_redemptions', ['code'])
    op.create_index(
        'ix_code_redemptions_redeemer', 'code_redemptions', ['redeemer_user_id']
    )
    op.create_index(
        'ix_code_redemptions_referrer', 'code_redemptions', ['referrer_user_id']
    )

    op.create_table(
        'email_sends',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('to_email', sa.String(255), nullable=False),
        sa.Column('template', sa.String(64), nullable=False),
        sa.Column('category', sa.String(16), nullable=False),
        sa.Column('dedupe_key', sa.String(255), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        # How many times the provider call has been attempted for this key. The
        # dedupe row is NEVER deleted (that would reopen the double-send race),
        # so this is what lets a send that FAILED be retried without letting a
        # send that SUCCEEDED be repeated: a claim may be re-taken only while
        # status is 'failed' and attempts is below the retry ceiling.
        sa.Column(
            'attempts', sa.Integer(), nullable=False, server_default='0'
        ),
        sa.Column('provider_message_id', sa.String(255), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dedupe_key', name='uq_email_sends_dedupe_key'),
    )
    op.create_index('ix_email_sends_user_id', 'email_sends', ['user_id'])
    op.create_index('ix_email_sends_to_email', 'email_sends', ['to_email'])

    op.create_table(
        'email_suppressions',
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('reason', sa.String(32), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('email'),
    )


def downgrade() -> None:
    """Drops all four tables.

    This DOES lose data that cannot be recovered from anywhere else: which
    accounts had already taken a discount, and which addresses had unsubscribed.
    Losing the suppression list is the serious one — re-emailing someone who
    unsubscribed is a CAN-SPAM violation, not just an annoyance. Export
    email_suppressions before running this against an environment that has ever
    sent promotional mail.
    """
    op.drop_table('email_suppressions')
    op.drop_index('ix_email_sends_to_email', table_name='email_sends')
    op.drop_index('ix_email_sends_user_id', table_name='email_sends')
    op.drop_table('email_sends')
    op.drop_index('ix_code_redemptions_referrer', table_name='code_redemptions')
    op.drop_index('ix_code_redemptions_redeemer', table_name='code_redemptions')
    op.drop_index('ix_code_redemptions_code', table_name='code_redemptions')
    op.drop_table('code_redemptions')
    op.drop_index('ix_referral_codes_code', table_name='referral_codes')
    op.drop_index('ix_referral_codes_user_id', table_name='referral_codes')
    op.drop_table('referral_codes')
