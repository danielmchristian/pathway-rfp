import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UpdatedAtMixin


class RfpRequestStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"
    partial = "partial"
    closed = "closed"


class EmailDirection(str, enum.Enum):
    out = "out"
    # `in` is a Python keyword; the member name is `in_` but the DB value is `in`.
    in_ = "in"


class EmailStatus(str, enum.Enum):
    queued = "queued"
    sent = "sent"
    failed = "failed"
    received = "received"


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Store enum *values* (not Python member names) in Postgres."""
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
    )


class RfpRequest(Base, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "rfp_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[RfpRequestStatus] = mapped_column(
        _pg_enum(RfpRequestStatus, "rfp_request_status"),
        nullable=False,
        default=RfpRequestStatus.draft,
        server_default=RfpRequestStatus.draft.value,
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["RfpRequestItem"]] = relationship(
        back_populates="rfp_request", cascade="all, delete-orphan"
    )
    emails: Mapped[list["RfpEmail"]] = relationship(
        back_populates="rfp_request", cascade="all, delete-orphan"
    )


class RfpRequestItem(Base):
    __tablename__ = "rfp_request_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    rfp_request_id: Mapped[int] = mapped_column(
        ForeignKey("rfp_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str | None] = mapped_column(String(40))

    rfp_request: Mapped["RfpRequest"] = relationship(back_populates="items")


class RfpEmail(Base, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "rfp_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    rfp_request_id: Mapped[int] = mapped_column(
        ForeignKey("rfp_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    distributor_id: Mapped[int] = mapped_column(
        ForeignKey("distributors.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    direction: Mapped[EmailDirection] = mapped_column(
        _pg_enum(EmailDirection, "email_direction"),
        nullable=False,
    )
    subject: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)
    # RFC-822 Message-ID string from the email envelope; not the PK.
    message_id: Mapped[str | None] = mapped_column(String(500), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(500), index=True)
    status: Mapped[EmailStatus] = mapped_column(
        _pg_enum(EmailStatus, "email_status"),
        nullable=False,
        default=EmailStatus.queued,
        server_default=EmailStatus.queued.value,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)

    rfp_request: Mapped["RfpRequest"] = relationship(back_populates="emails")
