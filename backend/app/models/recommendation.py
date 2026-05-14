from decimal import Decimal

from sqlalchemy import Boolean, Float, ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin


class Recommendation(Base, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "recommendations"

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
    score: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    # Phase 6 — honesty flags so the demo writeup can't gloss over a
    # comparison that wasn't apples-to-apples.
    incomplete_comparison: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    coverage_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    component_breakdown: Mapped[dict | None] = mapped_column(JSONB)
