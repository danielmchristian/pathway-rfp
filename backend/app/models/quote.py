from decimal import Decimal

from sqlalchemy import Float, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin


class Quote(Base, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "quotes"

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
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str | None] = mapped_column(String(40))
    min_order_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    delivery_days: Mapped[int | None] = mapped_column()
    terms: Mapped[str | None] = mapped_column(Text)
    source_email_id: Mapped[int | None] = mapped_column(
        ForeignKey("rfp_emails.id", ondelete="SET NULL"),
        index=True,
    )
    parse_confidence: Mapped[float | None] = mapped_column(Float)
    missing_fields: Mapped[list[str] | None] = mapped_column(ARRAY(String))
