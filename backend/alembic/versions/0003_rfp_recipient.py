"""rfp_emails: recipient_actual / recipient_nominal / resend_id

Revision ID: 0003_rfp_recipient
Revises: 0002_pricing_cols
Create Date: 2026-05-14

Phase 5 — record both nominal distributor address and the actual demo
override (`daniel+slug@…`) on each outbound email, plus Resend's response
id (separate from the RFC-822 Message-ID we set ourselves).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_rfp_recipient"
down_revision: str | None = "0002_pricing_cols"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rfp_emails",
        sa.Column("recipient_actual", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "rfp_emails",
        sa.Column("recipient_nominal", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "rfp_emails",
        sa.Column("resend_id", sa.String(length=120), nullable=True),
    )
    op.create_index(
        "ix_rfp_emails_resend_id",
        "rfp_emails",
        ["resend_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_rfp_emails_resend_id", table_name="rfp_emails")
    op.drop_column("rfp_emails", "resend_id")
    op.drop_column("rfp_emails", "recipient_nominal")
    op.drop_column("rfp_emails", "recipient_actual")
