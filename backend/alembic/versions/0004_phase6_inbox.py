"""Phase 6: inbox monitor, follow-up cap, recommendation breakdown

Revision ID: 0004_phase6_inbox
Revises: 0003_rfp_recipient
Create Date: 2026-05-14

Phase 6 schema delta:

  * rfp_emails: + is_followup, attribution_method, parse_status.
  * Amendment A — PARTIAL UNIQUE INDEX enforces "max one follow-up per
    (distributor, rfp_request)" at the DB level so the cap holds even
    across concurrent inserts. The application catches the resulting
    IntegrityError and logs `followup.skipped.cap_reached`.
  * imap_seen_uids: idempotency for IMAP polling. UNIQUE(mailbox,
    uid_validity, uid). Amendment B — populated in the same transaction
    as the corresponding rfp_emails row so a crash mid-poll cannot
    silently lose a reply.
  * recommendations: + incomplete_comparison, coverage_pct,
    component_breakdown. Honest signal that a basket comparison wasn't
    apples-to-apples.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0004_phase6_inbox"
down_revision: str | None = "0003_rfp_recipient"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----- rfp_emails additions -----------------------------------------
    op.add_column(
        "rfp_emails",
        sa.Column(
            "is_followup",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "rfp_emails",
        sa.Column("attribution_method", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "rfp_emails",
        sa.Column("parse_status", sa.String(length=40), nullable=True),
    )

    # F2 — unattributed inbound replies need NULL FKs. The original schema
    # had them NOT NULL because outbound rows always have both. Phase 6
    # relaxes for inbound rows; the application is responsible for setting
    # them when attribution succeeds.
    op.alter_column("rfp_emails", "rfp_request_id", nullable=True)
    op.alter_column("rfp_emails", "distributor_id", nullable=True)

    # Amendment A — partial unique index. DB-enforced follow-up cap.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_one_followup_per_dist_rfp
        ON rfp_emails (rfp_request_id, distributor_id)
        WHERE is_followup = true
        """
    )

    # ----- imap_seen_uids (new table) -----------------------------------
    op.create_table(
        "imap_seen_uids",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mailbox", sa.String(length=120), nullable=False),
        sa.Column("uid_validity", sa.BigInteger(), nullable=False),
        sa.Column("uid", sa.BigInteger(), nullable=False),
        sa.Column(
            "seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "rfp_email_id",
            sa.Integer(),
            sa.ForeignKey("rfp_emails.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "mailbox", "uid_validity", "uid", name="uq_imap_seen_uid"
        ),
    )
    op.create_index(
        "ix_imap_seen_uids_mailbox_uid",
        "imap_seen_uids",
        ["mailbox", "uid"],
    )

    # ----- recommendations additions ------------------------------------
    op.add_column(
        "recommendations",
        sa.Column(
            "incomplete_comparison",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "recommendations",
        sa.Column("coverage_pct", sa.Numeric(5, 2), nullable=True),
    )
    op.add_column(
        "recommendations",
        sa.Column("component_breakdown", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recommendations", "component_breakdown")
    op.drop_column("recommendations", "coverage_pct")
    op.drop_column("recommendations", "incomplete_comparison")

    op.drop_index("ix_imap_seen_uids_mailbox_uid", table_name="imap_seen_uids")
    op.drop_table("imap_seen_uids")

    op.execute("DROP INDEX IF EXISTS ix_one_followup_per_dist_rfp")
    op.alter_column("rfp_emails", "rfp_request_id", nullable=False)
    op.alter_column("rfp_emails", "distributor_id", nullable=False)
    op.drop_column("rfp_emails", "parse_status")
    op.drop_column("rfp_emails", "attribution_method")
    op.drop_column("rfp_emails", "is_followup")
