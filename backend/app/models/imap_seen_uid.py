"""Phase 6 — IMAP UID idempotency tracking.

Populated in the SAME transaction as the corresponding rfp_emails row
(Amendment B) so a crash between IMAP fetch and rfp_emails INSERT cannot
silently lose a reply. The UNIQUE constraint on (mailbox, uid_validity,
uid) is the F1 safety net.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ImapSeenUid(Base):
    __tablename__ = "imap_seen_uids"
    __table_args__ = (
        UniqueConstraint("mailbox", "uid_validity", "uid", name="uq_imap_seen_uid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox: Mapped[str] = mapped_column(String(120), nullable=False)
    uid_validity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    rfp_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("rfp_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
