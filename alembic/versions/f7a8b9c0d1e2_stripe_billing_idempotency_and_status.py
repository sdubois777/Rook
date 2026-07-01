"""stripe_billing_idempotency_and_status

Adds the Stripe billing support surface:
  - users.subscription_status (nullable) — billing state for the UI
  - processed_stripe_events   — webhook idempotency layer 1 (per event.id)
  - granted_monthly_invoices  — webhook idempotency layer 2 (per invoice.id)

Revision ID: f7a8b9c0d1e2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('subscription_status', sa.String(length=20), nullable=True),
    )

    op.create_table(
        'processed_stripe_events',
        sa.Column('event_id', sa.String(length=255), nullable=False),
        sa.Column(
            'seen_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('event_id'),
    )

    op.create_table(
        'granted_monthly_invoices',
        sa.Column('invoice_id', sa.String(length=255), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('credits', sa.Integer(), nullable=False),
        sa.Column(
            'granted_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('invoice_id'),
    )
    op.create_index(
        op.f('ix_granted_monthly_invoices_user_id'),
        'granted_monthly_invoices',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_granted_monthly_invoices_user_id'),
        table_name='granted_monthly_invoices',
    )
    op.drop_table('granted_monthly_invoices')
    op.drop_table('processed_stripe_events')
    op.drop_column('users', 'subscription_status')
